# Approved new cleanup diagnostic one-shot after consumed PR40 attempt

The user separately approved one new diagnostic trial within the same narrow
KVM grant/restoration and startup scope. This local active candidate still
requires exact-byte independent review before publication. PR40 run40267046975 reported successful
narrow startup assertions and its session cleanup, but overall run failed:
ACL restoration was unconfirmed in both main and fallback cleanup. The artifact
contained no restore code or transaction status, so the actual ACL failure cause
is UNKNOWN. Its frame was Android's splash screen, not proof of Inventory UI
readiness. `uiReadinessChecked: false` now makes that boundary explicit.

Local fake tests exercise complete basic/extended restore serialization and
command exit/readback/state failures. Fixed allowlisted stage/code/status fields
now distinguish future cleanup failures without exposing ACLs, UID, paths, tokens
or exception text. No guard, privileged target or authority behavior is weakened.
A reproducible fake process-group disappearance race explains how a transient
primary emulator-cleanup false negative can occur; bounded read-only disappearance
checking addresses that possible issue, not a proven root cause of PR40.

The new diagnostic trial preserves the consumed trial's scope below; it is not a
retry of either PR39 or PR40. No automatic retry is permitted.

# New Android startup one-shot: narrow KVM ACL candidate

The consumed trial was a new one-shot, not a rerun of PR39. PR39's only attempt stopped
before SDK/emulator startup for lack of existing KVM access. The new exact head
branch is `codex/android-startup-cleanup-diagnostic-once`; workflow
`android-startup-cleanup-diagnostic.yml` accepts only same-repository PR opened event,
attempt 1, with one Ubuntu 24.04 job bounded to 25 minutes. The false barrier is removed for this separately approved new one-shot; final
active-byte review and publisher authorization remain required before upload. No automatic retry, push/synchronize or
workflow_dispatch trigger is permitted.

The user separately approved one fresh trial with only the temporary runner UID's
named `/dev/kvm` read/write ACL grant and restoration. Preparation of this local candidate does
not itself publish or run that trial. No real ACL, emulator or native test was
executed during preparation.

## Narrow ACL boundary

Existing read/write access is used without mutation. Otherwise the helper accepts
only the fixed canonical character device `/dev/kvm`, bound to its original
inode/device identity, owner/group and mode. Before mutation it saves a bounded
validated original ACL in a private owner-only restore state and registers pending
restoration. Only the numeric effective runner UID gets a named `rw` entry.

For an extended ACL, the original mask must already include read/write and stays
exactly unchanged. For a basic ACL consisting of exactly owner/group/other, the
original group bits must include read/write; an explicit mask exactly equal to
those bits may be introduced together with the runner entry. This preserves the
original group-class mode and everyone else's effective rights. No mask expansion,
owner-UID bypass, malformed/default ACL, unrelated ACL-entry change, chmod, group,
udev or global setting change is accepted. Fixed `sudo -n setfacl` arguments use
`--no-mask`; restore uses only validated ACL entries, never ownership comments.
The privileged target is a held descriptor for the verified fixed KVM inode,
addressed by a derived /proc/self-owner-pid/fd/descriptor path, not an arbitrary
caller path. Path replacement cannot redirect the mutation to another inode. Canonical
identity is also bound to the kernel-reported /sys/class/misc/kvm/dev number.

Grant readback must match the exact expected ACL and original metadata; restore
must match the full original ACL and metadata, removing any introduced mask and
runner entry. Restore is attempted after errors/cancellation and independently
of emulator cleanup; failed/uncertain restore prevents cleanup confirmation and
retains private restore state for the always-step fallback. No private restore
state or ACL text is uploaded. Signals, forcible runner loss or irrecoverable
filesystem/device loss may prevent in-process restoration. A durable in-flight
privileged mutation with no proven completion is never raced by fallback
restoration; that state remains explicitly uncertain. Evidence never claims
success in that case, and ephemeral hosted-runner teardown remains a backstop.

## Unchanged startup scope

Production source/settings and existing causal-proof pins are unchanged. Only
manifest-pinned Android files are copied to a private build, where the exact
reviewed runtime-fixture include is appended. Only owned fixture/live debug APKs
are used. The test checks authenticated handshake, installed identity, owned
launch and one fresh synthetic frame, with zero UI input commands. Canonical APK,
TMPDIR and evidence destinations are validated before side effects. Production
startup deadlines and authority semantics are unchanged.

Only explicit sanitized JSON and one synthetic JPEG are retained for 7 days. No raw
exceptions, tokens, payloads, native logs, signing keys, homes, authority database
or ACL backup is published. Session/helper/authority/owned-forward cleanup and
emulator/process-group/reaping/owned-AVD/private-files cleanup retain truthful
independent statuses. ACL restoration is another independent status.

Process cleanup evidence excludes proving termination of a localhost ADB server
that adb devices may start. Disposable hosted-runner teardown is its backstop;
no global adb kill-server is used. Official actions/upload-artifact@v4 remains a
standard mutable dependency, not claimed SHA-pinned.

The fixture's four files match public source commit
`6eb207897f5819bf74b949c062a88e67df810abd`. The earlier dormant local draft was
reconstructed after executor rollback rather than claimed exact-byte restored.
This candidate is newly hashed/tested/reviewed; portable fake tests are not proof
of native runtime or live ACL success.
