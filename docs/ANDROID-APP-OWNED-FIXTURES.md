# Android Debug app-owned preparation (opt-in, runtime unqualified)

This is a general app-owned fixture path, not the v1 numeric repair profile.
It uses runtime-application profiles and registered prepare/check/cleanup
recipes. No numeric oracle, edit location, or sample target IDs are required
for preparation. Scenario replay still requires the existing approved scenario,
observation adapters, and independent bug/expected predicates for a verdict.
An app acknowledgment is **not** a defect verdict or proof of backend determinism.

The implementation is Android-first and has synthetic host/protocol coverage.
Native compilation and Android runtime/device qualification are distinct gates;
compilation alone does not qualify this Binder channel on a device. iOS has no
corresponding app-owned SDK channel in this change.

## Supported boundary

- Shared host authority and a dedicated, reserved Android device are required.
  No legacy-offline fallback or prepared-native authority exception exists.
- A selected single, debuggable APK, Android API 28+, default application process,
  no shared UID, and no split APK are required. The helper must be signed with
  an **existing operator-controlled, trusted signing identity** whose public
  certificate SHA-256 is explicitly pinned. No credential/key is generated.
- Default `CN=Android Debug` helper identity, the known public AOSP testkey, and
  helper signing rotation/multiple signers are rejected. This subject check is not an exhaustive detector of all
  public/test private keys: the operator must not approve such a key.
- The first version supports one closed fixture, one preparation check, and one
  cleanup recipe per attempt. Payload is exactly `{}`. Automatic app-log startup
  and candidate/retained protected repair are not supported by this path yet.
- Ordinary direct/reviewed-import playback remains available without any SDK.
  It stays unverified and never acquires a prepared start from imported receipts.

## App author integration

Use the SDK only in a Debug/QA variant. See the non-shipping sketches under
`android/sdk/examples/debug/`. Do not add the service to the release manifest.

1. Before normal app bootstrap, hold `ReproofFixtureBootstrapGate.holdForQa(this)`.
   Ordinary startup entry points must consult `normalStartupAllowed`. Keep the
   gate held throughout the QA session; the tool does not release it into normal
   business initialization when playback begins. Keep the main thread responsive
   and show a harmless QA waiting screen; do not block Application/Activity
   startup waiting for a Binder preparation that has not arrived yet.
2. Register the held gate, fixture ID/version, policy digest, existing helper
   signer fingerprint, and `ReproofFixtureHandler` using `ReproofFixtures.install`.
3. Implement bounded `prepare`, `check`, and `cleanup`. Each handler must check
   `attempt.ensureActive()` between steps and return only after all work it owns
   is quiescent. Do not leave detached threads/coroutines/network work running.
   A hanging handler causes cancellation to remain pending and retains quarantine.
4. Add the fixed `io.reproof.sdk.ReproofFixtureService` only to the Debug manifest,
   exported for the helper connection, without intent filters or another process.
   The SDK authenticates each Binder request; an exported component is not an
   invitation to accept arbitrary callers.
5. Select a QA-only build and explicitly approve its bootstrap/network policy.
   The SDK requires an immutable process-lifetime held gate, but cannot prove
   arbitrary Application/content-provider initializers or detached work honored
   it before or after registration. The tool does not rewrite production URLs, disable
   business controls, clear app data, reinstall as a reset, or reset permissions.

The policy digest must describe the actual fixture implementation and its
assumptions, including any response fixtures or backend checks. If live network
state is not fixed, do not describe the entire starting environment as deterministic.

## Trusted Python composition

This first integration is a trusted local Python composition API, not a new
ordinary Live endpoint or a JSON plugin loader. Prepare the runtime profile
with `approvedReferences.preparations` containing the selected reference. First
choose the semantic fixture policy and construct the G0 recipes using a pure
endpoint identifier (this helper grants no authority and needs no profile):

```python
from reproof.android_readiness import android_fixture_endpoint_for_policy
endpoint_id = android_fixture_endpoint_for_policy(
    fixture_id="empty_dashboard", fixture_version=1,
    policy_digest=approved_fixture_policy_digest,
)
```

Declare prepare/check/cleanup recipes with that `endpointId`, `exclusive=True`,
and capabilities `{remoteFencing: True, terminalStatus: True,
idempotencyRetentionMs: 60000}`. Build the selected APK and finalize the G0
project, then generate the runtime profile against that exact project digest.
Only now validate the build-bound selection:

```python
from reproof.android_readiness import validate_android_fixture
from reproof.live.android_preparation import AndroidFixtureAdapter
from reproof.live.issue_sessions import FixturePreparation

selection = validate_android_fixture({
    "schemaVersion": 1,
    "kind": "reproof-approved-android-fixture",
    "referenceId": "qa_seed",
    "profileDigest": runtime_profile.digest,
    "artifactDigest": runtime_profile.data["artifact"]["sha256"],
    "fixtureId": "empty_dashboard",
    "fixtureVersion": 1,
    "policyDigest": approved_fixture_policy_digest,
    "helperSignerDigest": approved_existing_helper_certificate_digest,
    "launchConfigurationDigest": approved_launch.digest if approved_launch else None,
}, runtime_profile, approved_launch)
adapter = AndroidFixtureAdapter(selection)
```

The previously declared prepare/check/cleanup recipes must match
`adapter.endpoint_id` and `adapter.capabilities.wire()` exactly. Their normal
recipe product-file declarations remain ordinary project metadata, not paths
read by the SDK. The endpoint is derived from fixture ID/version/policy, so
fixture equivalence cannot silently substitute a different preparation contract.

```python
registration = lab.register_recording_project(project, collection_policy)
plan = fixtures.register_plan(
    registration,
    application_id=runtime_profile.data["applicationId"],
    fixture_id="empty_dashboard",
    check_recipe_ids=("check_empty_dashboard",),
    cleanup_recipe_id="cleanup_empty_dashboard",
    adapter=adapter,
)
service = lab.create_issue_session_service(fixtures,
    scenario_registry=scenario_registry, scenario_runner=scenario_runner)
preparations = (FixturePreparation(plan, {}),)
handle = service.start_prepared_recording(
    device_id=device_id, owner=owner, controller_id=controller_id,
    registration=registration,
    application_id=runtime_profile.data["applicationId"],
    build_id=runtime_profile.data["buildId"],
    preparations=preparations, authority_grant=authority_grant,
)
# Use service.input(...) and service.stop(handle), not raw provider calls.
```

The exact registered Android adapter selects the dedicated preparation path.
`service.start_android_prepared_recording(..., preparation=preparations[0],
configuration=selection)` is also explicit. The same-build `service.replay(...)`
path uses the same selection and creates a fresh attempt after prior cleanup.
It never reuses the original readiness receipt as current preparation proof.
A failed/unknown attempt is not automatically retried or rebound.

## Trust and lifecycle

Each attempt gets a random nonce and binds session, device scope, ownership
generation, project/build, installed artifact, runtime profile, fixture/version,
policy, approved launch digest, and host/helper/provider incarnations. Every
phase also binds allocation generation, recipe, operation ID, idempotency key,
and closed payload digest. Typed Binder messages have fixed methods and limits;
there are no file paths, URI grants, shell commands, broadcasts, or callbacks
loaded from configuration.

The helper validates the selected installed APK and fixed service/UID. The app
validates the helper UID and pinned current signer on every Binder ingress.
The helper validates the kernel caller UID on every terminal callback before
processing it. Echoed JSON/digests alone never establish app identity. These
checks trust the OS, host, approved app implementation, and existing signing key;
they are not hardware attestation against a compromised host/root device.

Preparation occurs with recorder, frame publication, and user input closed.
Only authenticated terminal prepare/check results can produce a one-use,
nonserializable process-owned handoff. Identity/authority are checked again,
the recorder is created, and the same provider is activated without relaunch.
The native frame clock is rebound to that recorder before frames are admitted.

Stopping first closes native input admission and cancels held gestures under a
current permit. App cancellation must reach an authenticated quiescent terminal
before cleanup can run. Cleanup must complete before helper/device release.
Timeout, lost ACK, unbind, process death, server restart, and helper disconnect
are **not** proof of quiescence; uncertain work retains quarantine and exclusion.
Late success cannot reactivate a cancelled/expired attempt. No imported receipt,
ordinary reset, or edited recording can mint this handoff or bypass T0.
