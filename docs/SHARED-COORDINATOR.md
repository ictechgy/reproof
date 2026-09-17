# Shared coordinator administration (G5)

G5 adds an authenticated shared coordinator while preserving the original
single-user console. The modes are deliberately separate:

- `live-serve` without `--shared-config` remains loopback-only and uses the
  legacy `repro_live` cookie and `local-owner` namespace.
- `live-serve --shared-config ...` never issues or accepts that owner cookie.
  Every scoped API request needs a current principal credential or browser
  session, and every resource is resolved through a locally registered project.
- A shared non-loopback listener requires a TLS certificate and private key.
  HTTP is accepted only on loopback for synthetic/local integration.

The configuration format is `reproloop-shared-coordinator` schema version 2.
See [the non-secret example](examples/shared-coordinator-v2.json). The
`coordinator-v2` state root is an isolated, service-owned SQLite namespace; it
does not reuse or rewrite the G1 host-authority database.

## Capability matrix

Project roles are additive, not hierarchical. Grant each identity only the
viewer, operator, or maintainer roles it needs. Administrator is a separate
explicit identity flag and is never a project membership.

| Capability | Viewer | Operator | Maintainer | Administrator identity |
| --- | --- | --- | --- | --- |
| Project/device/session/recording/job reads | yes | yes | yes | no implicit project access |
| Export and reports | yes | no | yes | no implicit project access |
| Device input, session control, evidence collection, replay, fixtures, jobs | no | yes | no | no implicit project access |
| Project/specification maintenance, import and derive | no | no | yes | no implicit project access |
| Identities, memberships, credentials, host enrollment/revocation, assignment, legacy adoption | no | no | no | yes |

Mutating a session, recording replay, or job also requires ownership. Reads are
filtered by project membership. Viewer reads use stored state and do not renew
session authority or invoke observation/log providers. Roles may be combined;
for example, an operator who also exports evidence needs viewer or maintainer.

## Local administrator CLI

Administrator secrets are read from stdin. New principal and enrollment
secrets are written once to a new mode-0600 file and are never printed. Do not
commit these files. The paths and identities below are examples, not secrets.

Create the isolated store and its first bounded administrator credential:

```sh
python3 -m reproloop live-admin init \
  --state-root /var/lib/reproloop/coordinator-v2 \
  --administrator release-admin \
  --lifetime-seconds 3600 \
  --output /run/reproloop/admin-credential.json
```

Register the exact project revision that shared startup will load, then create
identities and memberships:

```sh
python3 -m reproloop live-admin project-register \
  --state-root /var/lib/reproloop/coordinator-v2 \
  --credential-stdin \
  --project /etc/reproloop/checkout-project.json \
  < /run/reproloop/admin-credential.json

python3 -m reproloop live-admin identity-add \
  --state-root /var/lib/reproloop/coordinator-v2 \
  --credential-stdin --identity qa-operator \
  < /run/reproloop/admin-credential.json

python3 -m reproloop live-admin membership-grant \
  --state-root /var/lib/reproloop/coordinator-v2 \
  --credential-stdin --project checkout \
  --identity qa-operator --role operator \
  < /run/reproloop/admin-credential.json

python3 -m reproloop live-admin credential-issue \
  --state-root /var/lib/reproloop/coordinator-v2 \
  --credential-stdin --identity qa-operator \
  --lifetime-seconds 3600 \
  --output /run/reproloop/qa-operator-credential.json \
  < /run/reproloop/admin-credential.json
```

Create a host-scoped, single-use enrollment and deliver the resulting private
file through the deployment's credential channel:

```sh
python3 -m reproloop live-admin host-enrollment-create \
  --state-root /var/lib/reproloop/coordinator-v2 \
  --credential-stdin --host-id mac-worker-01 \
  --project checkout --trust-group qa \
  --lifetime-seconds 600 --credential-lifetime-seconds 86400 \
  --output /run/reproloop/mac-worker-01-enrollment.json \
  < /run/reproloop/admin-credential.json
```

The worker bootstrap stdin document contains `enrollmentToken` and the
existing transport-only `transportToken`; both values come from private
credential delivery. The host credential is also written privately:

```sh
python3 -m reproloop live-worker \
  --host 0.0.0.0 --port 9876 \
  --advertised-host worker-01.example.internal \
  --tls-cert /etc/reproloop/worker.crt \
  --tls-key /etc/reproloop/worker.key \
  --output /var/lib/reproloop/worker-output \
  --authority-root /var/lib/reproloop/worker-authority-v1 \
  --coordinator https://coordinator.example.internal:9443 \
  --coordinator-ca /etc/reproloop/coord-ca.pem \
  --host-id mac-worker-01 --host-incarnation boot-2026-09-12 \
  --enrollment-stdin \
  --host-credential-output /run/reproloop/mac-worker-01-host.json \
  --demo < /run/reproloop/mac-worker-01-bootstrap.json
```

Assign a known inventory device only after enrollment. Reassignment to another
project, trust group, or host requires a schema-version-1 sanitation receipt
whose `previousAssignment` exactly matches the durable current assignment.

```sh
python3 -m reproloop live-admin device-assign \
  --state-root /var/lib/reproloop/coordinator-v2 \
  --credential-stdin --device-id mac-worker-01--demo \
  --project checkout --host-id mac-worker-01 \
  < /run/reproloop/admin-credential.json
```

For reassignment, pass `--sanitation-receipt /path/to/receipt.json`; its exact
shape is:

```json
{
  "schemaVersion": 1,
  "kind": "device-sanitation-receipt",
  "deviceId": "mac-worker-01--demo",
  "previousAssignment": {
    "projectId": "checkout",
    "trustGroup": null,
    "hostId": "mac-worker-01",
    "hostGeneration": 1,
    "hostIncarnation": "boot-2026-09-12"
  },
  "completedAtMs": 1789156800000,
  "limitations": []
}
```

Revoke credentials, unconsumed enrollment tickets, memberships, and hosts with
`credential-revoke`, `host-enrollment-revoke`, `membership-revoke`, and
`host-revoke`. Revocation is checked again on later requests, every streamed
frame, job dispatch, and provider effect.

Start the coordinator after all configured projects and devices match durable
administration:

```sh
python3 -m reproloop live-serve \
  --shared-config /etc/reproloop/shared-coordinator-v2.json \
  --workers-stdin --output /var/lib/reproloop/coordinator-output \
  < /run/reproloop/coordinator-workers.json
```

`coordinator-workers.json` is a private stdin document with each worker's
transport token and current host credential. It is not part of public
configuration.

The coordinator selects a bounded G1 grant for the requested current project.
Prepared remote sessions reserve on the enrolled physical worker before fixture
effects and transfer that reservation into startup. Missing physical authority
is refused before resource binding. Qualified inventory, current host/profile
binding, and G1 ownership must all allow admission. See the
[G6 worker runtime guide](WORKER-RUNTIME.md) for profile configuration, recovery,
and artifact transport.

## Browser/API authentication

`POST /api/auth/session` accepts `{}` with a principal bearer credential and
returns a CSRF token while setting the HttpOnly `repro_shared` cookie. Browser
POST requests then send that cookie and the exact token in `X-Repro-CSRF`.
Bearer API clients may authenticate each request directly. Exact `Host` and
same-origin checks apply before route dispatch. Native `/bridge/...` requests
still require their provider credential; browser membership cannot replace it.

The secret-free request shapes for login and a locally assigned session are:

```http
POST /api/auth/session HTTP/1.1
Host: coordinator.example.internal:9443
Authorization: Bearer <privately-delivered-principal-credential>
Origin: https://coordinator.example.internal:9443
Content-Type: application/json

{}
```

```http
POST /api/sessions HTTP/1.1
Host: coordinator.example.internal:9443
Cookie: repro_shared=<browser-session-cookie>
X-Repro-CSRF: <csrf-token-from-login>
Origin: https://coordinator.example.internal:9443
Content-Type: application/json

{"deviceId":"assigned-local-device","clientId":"browser-01","projectId":"checkout","applicationId":"ios_app","buildId":"original"}
```

Supply the placeholders through the deployment credential mechanism; do not
paste their values into process arguments, shell history, examples, or
diagnostics.

The host enrollment API is limited to:

- `POST /api/hosts/enroll` with a one-time enrollment bearer and
  `{"hostId":"...","incarnation":"..."}`.
- `POST /api/hosts/authenticate` with a current host bearer and `{}`.
- `POST /api/hosts/authorize` with a current host bearer and
  `{"projectId":"checkout"}`. Enrolled workers call this before accepting a
  delegated project grant and reauthenticate before later non-cleanup requests
  and actual provider effects. A request admitted before revocation cannot
  wait for its body and then use the earlier authorization to inject input.

No shared route accepts a caller-supplied project ID as authority. Session,
recording, job, issue, media, export, and report IDs are looked up in the local
resource graph first. Imported recordings are bound as `legacy-inert` and
cannot execute.

## Legacy adoption

Adoption copies the original regular-file bytes unchanged, records SHA-256,
format, byte count, and limited meaning, and imports no grants, hosts, result
status, or fixture work:

```sh
python3 -m reproloop live-admin legacy-adopt \
  --state-root /var/lib/reproloop/coordinator-v2 \
  --credential-stdin --adoption-id checkout-recording-2026-09-12 \
  --source /var/lib/reproloop/import/recording.json \
  --format recording-v1 --meaning legacy-recording-only \
  < /run/reproloop/admin-credential.json
```

Execution remains denied until a separate local administrator binds the exact
imported recording digest to that adoption with
`legacy-recording-authorize`:

```sh
python3 -m reproloop live-admin legacy-recording-authorize \
  --state-root /var/lib/reproloop/coordinator-v2 \
  --credential-stdin --recording-id recording-imported-01 \
  --adoption-id checkout-recording-2026-09-12 \
  --recording-digest 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  < /run/reproloop/admin-credential.json
```

A historical `verified` field remains inert legacy data and never becomes
release or company verification.

When a project revision changes, keep any older project/policy pair needed to
read its immutable recordings in the `projects` configuration list alongside
the current pair. Historical pairs are accepted only when an existing durable
resource is already bound to their exact digest; only the current
administrator-registered revision can create new sessions or device effects.
Historical sessions can still be closed, and their replay jobs can still be
cancelled. These operations preserve the existing producer/fixture cleanup
requirements and do not grant new input authority.

## Acceptance boundary

G5 tests use owned temporary credentials, subprocesses, and loopback HTTP.
They cover cross-project IDs/lists, the role matrix, Host/CSRF checks,
unauthorized export/range/report paths, revocation during streams and jobs,
atomic enrollment consumption, stale credentials, incompatible namespaces,
and byte-preserving adoption. TLS deployment across two Macs, physical-device
operation, and company data remain environment acceptance and are not claimed.

The shared native CLI supports multiple registered projects through a fresh
grant for each selected project/session. Physical and two-Mac acceptance remain
separate from the software and same-Mac CLI checks described in the
[worker runtime guide](WORKER-RUNTIME.md).
