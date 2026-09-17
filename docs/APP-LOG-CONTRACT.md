# Automatic app observation log — schema v1

Implementation contract for Android/iOS automatic click, screen, and lifecycle observations. These observations are not executable replay steps. Replay may become invalid while observation logging continues. No product text, navigation titles, URLs, intent contents, arguments, or exception messages are allowed in this log.

## Storage and identity

Android base: `files/repro`. iOS base: `Library/Application Support/ReproLoop`.

- `app-log-session.json`: exact keys `schemaVersion`, `platform`, `applicationId`, `runId`, `sessionId`, `profileDigest`, `startedAtMs`.
- `app-logs/SESSION_UUID/app-log.json`: same identity keys plus `endSequence`, `truncated`, `lostEvents`, `events`.
- schemaVersion is integer 1. platform is `android` or `ios`. applicationId and profileDigest match the selected build/profile. runId and sessionId are canonical lower-case UUIDs. startedAtMs is integer wall time in milliseconds.
- Android host provides `repro_log_run_id` launch extra; iOS uses its existing `REPRO_RUN_ID`. Start only in authorized Debug record mode with matching fixture/profile. Invalid/missing run UUID disables this new log without affecting existing replay capture or product behavior.
- Log session UUID is owned by the logger and may differ from the replay capture UUID. Keep the log session across same-process Activity recreation when host runId is unchanged; a new host runId starts a new log.
- Write an initial empty log before publishing its identity marker. Atomic replacement, session-specific files, ordered background IO. Marker is immutable for that log session. Host pins full marker before and after snapshot collection. A snapshot is the latest durably written prefix; it is not a final replay boundary.
- Keep observations through background/foreground and replay invalidation. Do not claim process-kill persistence or OS termination callbacks are guaranteed.

## Events

Every event has EXACT keys: `seq`, `elapsedMs`, `type`, `name`, `component`, `componentId`, `target`.

- seq starts at 1 and is contiguous; endSequence equals event count. elapsedMs is nonnegative and nondecreasing monotonic elapsed time, at most 1,800,000ms.
- component: `application`, `activity`, `scene`, `view_controller`, or `view`.
- componentId: `app` or `c` followed by 16 lower-case SHA256 hex characters (hash of a static component class name; no descriptions/instance payloads).
- lifecycle names: `attached`, `created`, `started`, `resumed`, `paused`, `stopped`, `destroyed`, `save_state`, `foreground`, `background`, `active`, `inactive`, `connected`, `disconnected`, `termination_requested`. target is null.
- click names: `began`, `returned`, `threw`. target must be a configured button ID (Android tap targets; iOS tapTargets plus backTarget). Do not collect button labels. Emit around actual callback execution, including when strict replay capture is already invalid. Preserve callback count, result, and original exception.
- screen names: `appeared`, `disappeared`. target must be a configured screen name. Android adds optional profile `screenTargets` mapping existing root View resource IDs to screen names; iOS uses existing screenTargets. Observe visibility independently of clicks, deduplicate stable state. Activity/controller appearance also uses `c` + 16 hex class hash as target, so an unconfigured app screen still has a bounded opaque identity. Do not infer a semantic route from text/layout fingerprints.
- Max 2,000 events and 1 MiB complete JSON. On exhaustion retain a bounded prefix and mark truncated=true. On collector/IO loss use lostEvents=true when publishable; never silently label a missing suffix complete. No input values or arbitrary extension fields.

## Host/UI

Strict shared validation, selected run/profile/application identity checks, safe session paths, size bounds, immutable marker bookends. Allow a flagged prefix to be downloaded with its incomplete status; never convert it into a valid replay scenario. Existing replay/capture schema remains backward compatible. Live can view/download observations without an external model request. Repair may include a validated observation snapshot with its own digest; it cannot change executable replay coverage.
