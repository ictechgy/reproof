# Reproof Live console

Dependency-free ES module UI for the same-origin Live session service.

```bash
python3 -m reproof live-serve --demo
```

Open the printed loopback URL. The synthetic device goes through the real HTTP session, input, recording, and replay APIs; it is labelled Demo and never used as evidence of native device support. Native Simulator setup and capability limits are in [the Live runbook](../docs/LIVE-RUNBOOK.md).

Frames use the bounded binary `/stream` transport when available, with explicit fallback to the legacy `/frame` endpoint for servers that return 404 or an unsupported stream response. The console keeps only the newest frame while decoding and reports received FPS from unique frame IDs; it does not claim a native or WebRTC FPS. Input is serialized at pointer release and tied to the displayed frame geometry and controller epoch. Pending input is flushed before recording start/stop. Browser refresh restores the same tab's public session ID; credentials stay in an HttpOnly cookie.

Devices advertising `continuous-pointer` receive pointer down/move/up/cancel edges immediately, with moves coalesced to 30 Hz and at most five browser pointer slots. Other devices retain the existing tap, long-press, and swipe release behavior.

The API provides devices, sessions/control/input/frame/close, recording start/stop/detail/export/script, recording import/library/derivation, automation jobs, and replay/cancel. The UI keeps the active session recording separate from the selected library recording, polls jobs and live sessions without adopting stale heartbeat responses, and does not persist imported recording text in browser storage. JSON imports are limited to 5 MB and reject raw log fields. The UI uses only the supported provider actions. The console assets are served from the source checkout during development and are bundled into the Python wheel for installed use.


## Ticket references

The authenticated issue workbench has a reference-only ticket panel. Explicitly
export the displayed saved revision, enter a bounded ticket ID, then select
**Link exported package**. The panel shows the exact selected package ID, revision
and specification digest; **Refresh references** reads retained matching/stale
references and the count of unavailable references. Export, approval, replay and
repair remain separate actions. No external tracker is contacted.

The export choice is memory-only and clears on issue/project navigation, logout,
capability or saved-revision changes, detected retention loss and failed reference
requests. Refresh can discover a link whose response was interrupted. Reloading
never restores a package choice. See [ticket references](../docs/TICKET-REFERENCES.md).

Focused dependency-free Node checks run through:

```sh
python3 -m unittest tests.test_g7_browser_logic
```

The optional actual-browser smoke requires already installed Chromium and Python
Playwright, uses only owned synthetic loopback fixtures, and does not download
tools or silently skip missing prerequisites. It is not part of the standard-
library portable ticket-reference gate:

```sh
python3 -m unittest tests.test_ticket_reference_browser -v
```
