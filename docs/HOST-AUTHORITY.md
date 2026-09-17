# Durable host authority — G1a and G1b

2026-09-11 · storage and provider-boundary contract

## Delivered boundary

`reproloop.live.authority.HostAuthority` is the process-local composition root
for one durable SQLite authority journal and any canonical devices currently
owned by that process. It provides durable admission, generation fencing,
absolute parent-grant deadlines, monotonic renewal sequences, persisted replay
watermarks, provider-result receipts, uncertain-outcome quarantine, and
restart reconciliation.

The shared Live, Android, Simulator, physical-iPhone and worker dispatch paths
use this authority through the G1b adapters below. Incompatible legacy runners
are refused before mutation; explicit offline adapters retain the canonical
lease. This does not prevent an unrelated user, root process, standalone
ADB/Xcode tool, or unsupported service account from accessing a device.

## Clock and parent grants

`SuspendInclusiveClock` uses `mach_continuous_time` with
`mach_timebase_info` on macOS. Its boot incarnation is a digest of
`kern.bootsessionuuid`. If that identity is unavailable or empty, clock
construction fails; wall time is never used to estimate a boot identity or an
authority deadline. Linux uses `CLOCK_BOOTTIME` plus a digest of the kernel boot
ID. Unsupported clocks fail construction.

Clock sampling and exchange registration are serialized. Every observed boot
change, clock-identity change, read failure, or backward movement invalidates
all prior process-local samples and mappings. Returning to an earlier identity
or a later timestamp cannot revive them. A new trusted exchange is required.

Clock exchange integration is intentionally process-local:

```python
received = authority.clock_sync.sample()  # immediately after trusted receive
sent = authority.clock_sync.sample()      # immediately before trusted reply
mapping = authority.clock_sync.record_exchange(
    coordinator_clock_id="coordinator-clock",
    coordinator_send_ns=coordinator_send_ns,
    host_received=received,
    host_sent=sent,
    coordinator_receive_ns=coordinator_receive_ns,
    coordinator_uncertainty_ns=coordinator_uncertainty_ns,
    max_drift_ppm=approved_drift_bound,
)
parent_grant = authority.issue_parent_grant(
    mapping,
    grant_id="parent-grant",
    project_id="project-one",
    controller_id="controller-one",
    renewal_sequence=1,
    coordinator_deadline_ns=absolute_coordinator_deadline_ns,
)
```

The coordinator timestamps must come from the authenticated trusted
coordinator adapter; imported packages cannot call this path or turn a mapping
document into authority. Translation returns an interval. Dispatch uses its
earliest host deadline, widened for exchange uncertainty and the declared drift
bound. Receiving a grant never restarts a TTL. A renewal must have the same
grant/project/controller binding and a strictly greater sequence while the old
grant is still current. An operation deadline is fixed at admission and remains
under its then-current parent ceiling even after a later parent renewal.

Boot changes, backward clock movement, unknown clocks, excessive uncertainty,
expired mappings, stale renewals, and grants issued by another `HostAuthority`
fail closed.

## Canonical lock compatibility and G1b adoption

`claim_device` accepts a trusted inventory's physical identity only in process
memory. The authority never includes it or a display alias in SQLite, receipts,
fingerprints, `repr`, or errors. It acquires the existing `storage.Lease`
namespace exactly as current providers do:

| kind | value hashed by `storage.Lease` |
|---|---|
| `android` | physical Android serial |
| `ios-physical` | `ios-device:` plus physical UDID |
| `ios-simulator` | `ios-simulator:` plus Simulator UDID |

Changing an authority-state path or display alias therefore cannot create a
second physical lock. Production processes must use the same OS service account
and the default `tempfile.gettempdir()/reproloop-leases-{uid}` lock directory.
Per-user `flock` namespaces do not coordinate multiple service accounts.

G1b must stop the old standalone controller before shared-service cutover, then
replace each provider-owned `Lease(...)` with the already-held no-op context:

```python
device = authority.claim_device(
    device_kind="android",
    physical_id=trusted_inventory_physical_id,
    display_alias=non_authoritative_display_name,
    helper_incarnation="helper-one",
    parent_grant=parent_grant,
)

provider_lease = device.borrowed_lease()
with provider_lease:
    # Existing provider setup may use the context, but exiting it does not
    # reacquire or release the physical lock. DeviceAuthority remains owner.
    pass
```

The borrowed context becomes unusable when `DeviceAuthority.close()` releases
the one canonical lock. G1b must not create another `Lease` for that device.
Rollback to a legacy controller is permitted only after every authority handle
is cleanly released; quarantined ownership requires reconciliation first.
Shared mode must also enforce the canonical authority-state root. A custom
state path is a trusted local/test setting; it cannot recover another root's
unfinished ownership history.

## Admission and provider integration API

Callers must parameterize sensitive input before computing `payload_digest`.
Neither raw input nor a payload object is accepted by the journal.

```python
admission = device.admit_operation(
    operation_id="operation-one",
    payload_digest=parameterized_payload_digest,
    session_id="session-one",
    sequence=1,
    deadline_ns=optional_earlier_host_deadline_ns,
)

permit = device.prepare_dispatch(
    admission,
    provider_incarnation="provider-one",
)
# The operation is now durably "uncertain" and the device is quarantined.
# Invoke the provider outside authority/store/lab locks.
provider_result = invoke_registered_provider(permit)
device.confirm_operation(
    permit,
    ProviderResult("receipt-one", "succeeded", provider_result_digest),
)
```

`DispatchPermit` carries protocol version, operation and payload fingerprints,
project/session/controller identity, sequence, ownership generation,
host/helper/provider incarnation, and the conservative deadline. G1b must
preserve these fields through each native hop and translate the host deadline
to an equal-or-earlier native deadline before injection. Native integration
must recheck generation, incarnation, sequence, and deadline immediately before
each injection boundary; it is not part of G1a.

`dispatch_operation(..., callback=...)` is a convenience with the same
journal-before-callback behavior. No SQLite or device-handle mutex is held while
the callback runs. A blocked callback, exception, malformed return, `unknown`
result, process loss, or receipt-persistence failure leaves the device
quarantined. An exact terminal `ProviderResult` restores the same current
generation only while its parent deadline remains current.

`HostAuthority.record_provider_result(...)` is the trusted asynchronous receipt
API. It binds operation, generation, host incarnation, and provider
incarnation. A receipt for a reconciled or newer generation is persisted with
`binding=late`; it cannot complete or release the new ownership.

Operation IDs are immutable. Reuse with a different envelope or payload digest
is rejected. Per-generation/controller sequences are contiguous and their
watermark is durable, so eviction from the bounded process cache cannot admit a
replay. Terminal results remain in the bounded journal; at the hard operation
limit, admission fails instead of forgetting history.

An identical retry returns its first admission and deadline, including after a
parent renewal. Dispatch refuses a later operation while an earlier operation
in that generation is still queued or uncertain. A never-dispatched operation
can expire without expiring its renewed, still-valid parent grant. Parent
expiry is committed before a renewal rejection is returned. A grant from a new
clock epoch cannot renew ownership established in an old epoch.

## Restart and reconciliation

`DeviceAuthority.close()` terminalizes never-dispatched queued operations and
records an eligible device as released. A successful native handshake also
records that the handle has an attached helper. Such a handle requires
`confirm_native_cleanup(permit)` after the exact helper/pointer cleanup
operation has a terminal successful receipt. An ordinary successful input or
an unconfirmed cleanup cannot satisfy this requirement. Closing either the
handle or its HostAuthority before that confirmation quarantines the device
and keeps legacy rollback blocked. Revocation is journaled even when a clock
read fails; the last stored timestamp is retained in that case.

Any other restored state (`owned`, `expired`, or
`quarantined`) becomes or remains quarantined after the new process obtains the
canonical kernel lock. Kernel lock release or process disappearance alone never
restores dispatch.

`device.recovery_snapshot()` returns a process-local capability naming the
prior host/helper incarnation and every queued or uncertain operation. The
trusted recovery supervisor must independently establish:

- prior helper stop/exit evidence;
- `not-dispatched` disposition for each queued operation;
- a terminal `succeeded` or `rejected` disposition for an uncertain operation
  with a confirmed outcome, or `recovered` when restoration is confirmed and
  the original effect remains unknown;
- active-pointer cleanup evidence; and
- a different, fresh helper incarnation and handshake.

It records per-operation evidence with
`HostAuthority.record_operation_disposition`, combines it through
`record_reconciliation`, and calls `device.reconcile` with a fresh parent-grant
ID. The transaction appends the reconciliation and disposition digests,
terminalizes all unfinished operations, increments ownership generation, and
only then restores `owned`. Missing, extra, forged, stale, or nonterminal
evidence leaves quarantine intact.

Protected Android recovery uses `cleanup_pending=True` on this transition.
Borrowing native recovery descriptors first fences the original provider's
unconfirmed result in the journal. A racing result is then retained as a late
receipt and cannot reopen dispatch while restoration is running.
The new generation remains quarantined as `recovery-cleanup-pending`; normal
dispatch, another reconciliation, clock invalidation, revocation and handle
close cannot clear this hold. The fixed recovery owner retains the original
producer and device lock descriptions, removes the exact staged files, and
supplies a live cleanup capability to `RunStore.finish_mobile_recovery`.
Only after the run reservation is released does it mark that reconciled
device generation released and close its lease. Interrupted finalization can
resume from the bound authority reconciliation and fresh file/lock checks;
the private JSON state is not an executable cleanup capability.

`recovered` records completed restoration, without asserting whether the
original command executed. The restoration result and evidence digests live in
`reconciliation_dispositions`; the original operation's result digest,
provider identity, and all receipts remain unchanged. Later provider results
are retained as `late` receipts and cannot change this status or the new
generation. A provider cannot return `recovered`, and queued operations still
require `not-dispatched`.

## Store format and bounded state

The dedicated default store is
`tempfile.gettempdir()/reproloop-authority-{uid}/authority.sqlite3`, separate
from legacy output roots. SQLite uses WAL, foreign keys, `synchronous=FULL`,
`BEGIN IMMEDIATE` state transitions, and a verified per-connection page limit.
Schema format, minimum reader, and minimum writer are explicitly version 3.
Opening a recognized version 1 or 2 store migrates in one exclusive transaction.
Version 1 additionally rebuilds operation status constraints and reconciliation
dispositions. Version 3 fences older code that cannot enforce pending final
cleanup. Existing device
ownership, queued/uncertain operations, receipts, replay watermarks and history
are preserved with foreign keys enabled. Failure or process exit before commit
leaves the original journal available for retry. Unknown application IDs,
tables, formats, or reader/writer minima are rejected before service use.
Downgrading the resulting journal to version 1 or 2 code is unsupported.

The store bounds devices, operations, receipts, reconciliations, legacy
adoptions, text identifiers, and database pages. SQL values are parameterized.
`record_legacy_adoption` stores only an immutable artifact digest and the
limited semantics `lock-only` or `unverified-history`; it cannot turn a legacy
artifact into executable or verified evidence.

## Verification and remaining boundary

Run the G1a software gate without a device or network:

```bash
python3 scripts/release-check.py --goal G1A --effects filesystem,process
```

The gate covers injected sleep/restart/drift/uncertainty, real host clock
advancement, durable idempotency and replay watermarks, renewal ceilings,
expiry while queued, callback quarantine, late results, store version refusal,
borrowed-lease behavior, real cross-process contention, and abrupt owner-process
loss. Synthetic identities are generated test data.

The parent-host gate currently runs 37 tests, including the independent clock,
renewal, queue-order and close/claim concurrency regressions. See
`artifacts/qa-delivery/g1a-parent-review.md` and `g1a-release-final.json` for
the reviewed evidence. A restricted worker that cannot read the boot identity
cannot qualify the real clock; the parent performs that check on the host.

## G1b native and worker integration

Shared native descriptors use the private trusted inventory field `_authority`
and public `capabilities.authorityMode = "shared-v2"`. The embedding service
must construct the Lab with the existing process-local capabilities:

```python
lab = Lab(
    devices,
    output,
    authority=host_authority,
    parent_grant=verified_parent_grant,
)
```

`Lab.create_session` claims the canonical `DeviceAuthority` before constructing
or starting the provider. It then calls, in order,
`bind_authority(device_authority, provider_incarnation)` and
`start_authorized(session, lab, permit)`. Input, reset, pointer cleanup,
side-effecting SDK/app-log collection, replay and job dispatch use the same
handle through `execute_authorized`, `collect_*_authorized`, and the stable
operation ID. Close admits one bounded cleanup operation, calls
`close_authorized(permit)`, records its receipt, and invokes
`confirm_native_cleanup(permit)` before `DeviceAuthority.close()` can release
the physical lock. A missing binder or coordinator grant is a clean pre-mutation
rejection; a dispatched operation without an exact terminal receipt remains
uncertain and quarantined. `Lab.fail` also durably changes the quarantine reason
and revokes every already-issued process-local dispatch permit, so a late
success receipt is historical and cannot restore ownership or enable rollback.

Android and iOS helpers report protocol/helper/host/helper/provider/native
incarnations and a qualified suspend-inclusive native clock. The host calls
`bind_native_handshake(...)`, then `native_grant(permit, handshake)` to produce
the exact wire envelope. Android uses `elapsedRealtime` for grant expiry and
`uptimeMillis` only for input-event timestamps. Its per-operation grant is
thread-local, so a concurrent cleanup request cannot replace the authority of
an older gesture. Once an authenticated stop is accepted, ordinary native
effects are irreversibly disabled before stop waits for the gesture lock. Every
down/move/up, HOME down/up, text set/selection, reset
subprocess boundary and Report click rechecks the same live grant. The native
deadline applies a declared conservative clock-rate bound and mapping
uncertainty. iOS uses `mach_continuous_time`; unknown fields, duplicate keys
(including escaped-equivalent keys), and non-integral authority numbers are
rejected before decoding. Shared startup returns the
startup grant to XCTest, launches the target only while it remains live, and
requires a same-grant `started` acknowledgement before the host confirms the
startup journal entry. Uninterruptible XCTest effects remain uncertain until
their acknowledgement or confirmed helper termination.

In shared mode, terminal command, activation and stop responses echo the exact
typed authority envelope. The host compares every field, including payload and
operation fingerprints, generation, sequence and all incarnations; matching an
operation ID alone is not a terminal receipt.

Remote shared devices add `_remoteAuthority`. Before the worker session is
created, `RemoteProvider` performs the authenticated
`GET/POST /v1/authority/exchange` handshake. The worker records a bounded
parent-to-worker monotonic mapping, issues a one-use local `ParentGrant` whose
deadline is no later than the originating grant, consumes its delegation ID at
session creation, and uses that grant for the worker's one physical
`DeviceAuthority`. The originating operation ID remains the worker command ID.
Missing/expired origin authority, excessive clock uncertainty, old worker
versions, or replayed delegation IDs fail before device mutation. Non-loopback
transport still requires verified TLS under the existing worker contract.

## Executable compatibility and rollback gate

| mode | code | authority store | helper | native protocol | permitted use |
|---|---:|---:|---:|---:|---|
| `legacy-offline-v1` | 1 | 1 | 1 | 1 | explicit standalone/offline controller only |
| `shared-v2` | 2 | 3 | 2 | 2 | injected HostAuthority and verified parent grant |

Provider builders and both live CLIs accept only these two mode strings. Their
default is `shared-v2`; because the standalone CLI has no supplied coordinator
grant service, a native session started that way currently rejects before
provider construction. Existing standalone behavior requires the explicit
`--authority-mode legacy-offline-v1` cutover and must not coexist with the
shared service.

Each canonical legacy lease has a sibling bounded authority marker. A shared
claim writes `rollback-blocked` before durable ownership, then `shared` after a
clean claim. Only confirmed native/helper/pointer cleanup plus durable authority
release can write `legacy-allowed`. Legacy adapter mutation methods acquire that
same canonical lease themselves, so a different output directory or direct old
replay entrypoint cannot bypass a live or unresolved shared owner. Android,
Simulator and physical-iPhone install/run/stop paths use tracked nested leases;
an authority-integrated provider supplies the existing borrowed lease instead
of acquiring another handle. The legacy repair subprocess has no v2 native
envelope and is therefore rejected with `native_protocol_mismatch` before
capture or mutation when the session is `shared-v2`.

Lock files are opened relative to a verified owned directory with no symlink
following, and must be single-link regular files owned by the service account.
Cutover versions use exact integer typing; JSON booleans cannot alias version 1.

## G1b verification boundary

The registered checks are:

```bash
python3 scripts/release-check.py --goal G1B --effects filesystem,process,loopback,native-compile
python3 scripts/release-check.py --goal G1 --effects filesystem,process,loopback,native-compile
```

They cover the Python authority/provider/worker integration, actual loopback
worker transport, legacy lock contention, malformed native envelopes, native
cleanup release, and both native compilation commands. The Android command
compiles the live helper and the replay driver because they share the changed
profile parser. Swift and Kotlin executable probes exercise the actual parser
and effect-check functions. A restricted worker may be unable to bind the
loopback test sockets or Gradle's file-lock coordination socket; those checks
must run on the parent Mac rather than being disabled.

These checks did not qualify a physical device, boot a Simulator,
install or launch an application, establish a second-Mac deployment, or prove a
company QA fixture. No company coordinator descriptor/grant service, app,
fixture, second Mac, guest image, or AI-transfer policy was supplied. Successful
synthetic tests and unsigned helper compilation are software evidence only, not
native runtime or company acceptance.
