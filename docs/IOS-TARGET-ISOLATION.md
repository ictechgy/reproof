# iOS Live target boundary

Live capture and ordinary input belong to the selected application, not the
whole device. This applies to the sample, installed-bundle, general-profile,
and physical-iPhone paths using the same XCTest helper. A new helper build is
required. Both transport handshakes require the exact integer capability
`targetBoundaryVersion: 1` before accepting frames or advancing startup. Old
helpers without the marker are rejected with a rebuild instruction; `true`,
`1.0`, missing and unknown versions are not accepted. The marker denotes these
observational guards, not an atomic OS isolation guarantee.

## Fail-closed behavior

- Tap, long press, swipe and text check that the selected target is observed in
  `runningForeground` immediately before dispatch. They never fall back to
  SpringBoard. A known pre-dispatch failure returns `target_not_foreground`.
- The helper checks again after the effect. `target_changed_during_input` is
  **uncertain**, not a no-effect rejection. Host handling fails the session and
  quarantines the device through the existing authority path. Never retry the
  gesture automatically; it may already have opened an external application.
- Live capture uses `targetApplication.screenshot()`, not the global display.
  The helper checks foreground before and after capture, and again after
  encoding before publication. A failed check discards the image and stops the
  helper's capture session. There is no synthetic blank frame masquerading as
  an application observation, and no fresh-frame/replay success on that path.
- An unsuccessful command's receipt can be sent without waiting for another
  capture. Transport failure can still make its outcome uncertain. Successful
  ordinary commands retain the existing capture-before-acknowledgement order.
- `home` and `terminate` do not grant authority to control or capture the home
  screen, another application, or a resumed page. They can end this target-only
  capture session and lead to quarantine through the existing failure path.
  This is an intentional change from background/home-screen interaction, not a
  resumable device-wide browser mode. Do not bypass quarantine to resume.
- Target cleanup/retirement remains available without requiring foreground.
  It acts only on the selected application and keeps the existing authority
  and termination-evidence checks.

If Safari itself is selected, it remains the target application: these checks
cannot establish a website origin or stop Safari showing another tab or site.
A browser origin policy requires a separate browser provider. Do not treat a
Safari bundle profile as company-site isolation.

## Limits of the boundary

These are observation guards, not an atomic OS security boundary. Apple's
[application state documentation](https://developer.apple.com/documentation/xcuiautomation/xcuiapplication/state-swift.property)
states that state changes are updated asynchronously. An OS transition can
occur between checks or during a gesture. App/element-scoped
[screenshots](https://developer.apple.com/documentation/xcuiautomation/xcuiscreenshotproviding/screenshot())
represent what is onscreen; an overlay can cover the target. Pre/post checks do
not prove absence of every transient overlay or a switch-away-and-back race.
No claim is made that external pixels can never occur under those races.
Use dedicated QA devices and non-production accounts; stronger isolation needs
an app-owned capture/input contract or OS-provided atomic target binding.

The explicit sample-only `testCaptureFormats` diagnostic still captures the
whole screen. It is not a Live bridge path, requires its diagnostic opt-in and
requires the repository sample bundle. Do not use it for company applications.

## Validation

`tests.test_ios_target_isolation.TargetReceiptTests` exercises actual host
provider receipt classification and Lab quarantine with inert transports.
`TargetSourceWiringTests` checks the Swift call sites and no-global-capture
wiring. Both portable classes are registered in `NATIVE_CONTRACTS`.
`NativeTargetGuardExecutionTests` extracts the actual Swift guard methods and
runs fake closures when `swiftc` exists. A skip is not native validation.

Before claiming device support, compile the helper with Xcode and run an
explicitly authorized synthetic two-app Simulator test: reject already
background inputs, open a second app from a target gesture, block subsequent
inputs, observe capture failure without external frames, verify cleanup and
quarantine, and repeat with home, terminate and transport loss. No company
application, real account or physical device is needed for that test.
