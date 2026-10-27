# Ordinary action playback is not trusted replay

The Live console has two different paths:

* **Trusted replay** continues to require a supported, proven starting state. Its
  eligibility and the G7/G9 evidence/repair paths are not relaxed by this feature.
* **Unverified action playback** repeats reviewed actions from the current screen,
  without resetting the app or proving the starting state, defect reproduction,
  or fix verification. Its terminal status is `actions_played_unverified`, not
  `reproduced`, `verified`, or a test success.

Use **Record without reset** when a general app has no usable reset fixture.
Stop recording, then select **Preview current-session capture**. Enter any
recording variables first. Review every normalized action/payload (including
coordinates and actual text), the current live screen, and the warning. A
coordinate is not a verified UI target. Check the explicit acknowledgment and
select **Execute reviewed actions (unverified)** only if the effects are intended.
Actions may change app and server data. The tool has **not** verified that the
app is connected to QA or a non-production server; preparing that environment
remains the operator's responsibility.

The preview issues no input. The server returns a one-use plan valid for at most
30 seconds. Confirmation submits its token and the actual currently displayed
frame, which must still satisfy freshness and geometry checks. Changes to the
session, controller, epoch, action sequence, selected recording, or geometry
require a new preview. Streaming frame updates alone do not change the plan.
There is no automatic confirmation or retry after an uncertain request. Review
session status before deciding whether to start a new plan.

Plans and entered text remain in memory, not browser storage. Hiding/leaving the
page discards local approval; pending server plans expire. This is not a promise
that navigation stops an already-running operation. Use **Cancel action playback**
to request cancellation and inspect the resulting session status. A failed cancel
is reported as uncertain, never silently retried. Closing a session uses the normal
session teardown path.

## Two distinct unverified action sources

**Preview current-session capture** keeps the original strict admission rule:
only a complete recording captured directly in this currently open session by
this running server process is eligible. Imported, restored, and derived records
do not become current captures.

**Review saved / imported data as new actions** instead constructs a NEW,
ephemeral `reviewed-untrusted-action-plan` from owner-readable recording data.
This path is available only in local ordinary sessions. An imported or restored source remains
inert, unknown-start and non-replayable: no source trust registry, replay proof,
or permanent authorization is created. Its digest is only an integrity reference,
not an authenticated claim about where the actions came from. The recording schema
still requires events to carry `status: injected`, but for imported data this is
only an untrusted format field, NOT evidence that any historical input succeeded. The full actions
and current target are shown, and a separate untrusted-source acknowledgment is
required in addition to unknown-start approval. Neither preview sends input.

The source's claimed app, provider, device, and geometry must match the actual
current target exactly; this is a compatibility check, NOT proof of origin.
There is no coordinate scaling or silent target remapping. Native targets require
an independently available non-null application identity. A bundle-only identity
does not establish exact build equality. Synthetic demo use also requires an explicit application identity; it is not
native-device evidence. Unsupported actions and incomplete pointer traces are
rejected independently of any imported replay flags. Text values must be entered
explicitly as recording variables; raw imported text is not accepted.

Source changes, deletion, ownership loss, current-target/controller/authority
changes, expiry, or cancellation invalidate the plan. Review a new plan instead
of retrying an old token. Restart does not preserve pending plans or approvals.
The plan cannot reset the app, prepare a fixture, certify the server environment,
or make an imported recording usable as trusted replay or G7/G9 evidence.

## Scope and limits

Prepared/release sessions are excluded. Normal session ownership, host authority,
controller, capability, and device safety checks still apply; there is no authority
fallback or fixture-proof fabrication. A playback result cannot enter protected
repair or release verification merely because its actions completed.

Synthetic JavaScript tests cover approval, expiry, repeated clicks, stale responses,
navigation and cancellation. They do not establish native-device, company-app,
production-environment, or end-to-end UI compatibility.

Allowed actions are `tap`, `long_press`, `swipe`, `text`, `back`, and `pointer`.
Reset, home, and application lifecycle actions are excluded. Control cannot be
claimed while a playback worker is still alive; cancel and wait for it to finish.
