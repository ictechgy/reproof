# Ordinary action playback is not trusted replay

The Live console has two different paths:

* **Trusted replay** continues to require a supported, proven starting state. Its
  eligibility and the G7/G9 evidence/repair paths are not relaxed by this feature.
* **Unverified action playback** repeats reviewed actions from the current screen,
  without resetting the app or proving the starting state, defect reproduction,
  or fix verification. Its terminal status is `actions_played_unverified`, not
  `reproduced`, `verified`, or a test success.

Use **Record without reset** when a general app has no usable reset fixture.
Stop recording, then select **Preview actions from current screen**. Enter any
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

## Initial scope and limits

Only complete recordings captured directly in the same currently open session
and by this same running server process are eligible. Imported recordings, recordings restored after restart, and derived
recordings are not enabled for action playback in this initial implementation.
This is not a permanent ban on imported automation: a future implementation may
accept imported actions as data through a new exact operator-reviewed plan and
authorization, never by trusting imported proof flags.
A preview refusal does not promote an imported recording into trusted evidence.
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
