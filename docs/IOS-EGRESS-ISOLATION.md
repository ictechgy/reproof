# iOS target-app egress isolation

2026-09-20 · r68 design note — boundary that is still open

## Threat model

A protected run installs and launches a candidate build — machine-edited code
that did not exist when the issue was approved. Everything the verdict rests on
(recording, fixtures, observations, sanitation receipts) assumes the candidate
cannot reach off-device endpoints:

- **Exfiltration** — a patched binary can read the same sandbox the original
  used and push screen contents, keychain residue, or fixture payloads outward
  before cleanup runs.
- **Non-determinism** — a candidate that consults a remote endpoint makes the
  replay verdict depend on external state, breaking the approved-evidence
  contract the lifecycle is built on.
- **Lateral movement** — the QA phone sits on a real LAN. Outbound traffic is
  reachable infrastructure, not a simulator stub.

The Sample app itself makes no network calls (no `URLSession`/`NWConnection`
in protected-source). The gap is the *unproven boundary*: nothing currently
declares, measures, or attests egress for a candidate that could add it.

## Measured constraints (QA-iPhone, 2026-09-20)

| Enforcement point | Result |
|---|---|
| `devicectl` radio/network control | **No capability** — `device info details` lists no radio/Wi-Fi command surface |
| NEFilterDataProvider / per-app VPN | **Supervised only** — unavailable on an unsupervised device |
| `com.apple.proxy.http.global` profile | **Supervised only** — profile install exists (`configurationprofiles` capability) but the payload is gated |
| DNS settings profile | Not supervision-gated, but controls DNS only — direct-IP egress unaffected |
| `rvictl` Remote Virtual Interface | **Works** — `rvi0` created on the host for the attached device |
| `tcpdump`/BPF on rvi0 | **Root only** — no ChmodBPF installed; capture needs privilege |
| `getifaddrs` per-interface `if_data` counters | Available to any on-device app, no entitlement — usable by the helper |

Conclusion: on an unsupervised iPhone there is **no programmatic hard block**.
The honest design is declare + measure + attest, with a documented supervised
tier for real enforcement.

## Design — three layers

### 1. Declared policy (bound, not ambient)

New `egress-policy.json` in the protected configuration, digest-registered in
`mobile-definition.json` exactly like `runtimeProfile`/`sanitation`:

```json
{
  "kind": "ios-egress-policy",
  "version": 1,
  "mode": "deny-all",
  "interfaces": ["en0", "pdp_ip"],
  "noiseFloorBytes": 1048576,
  "allowlist": [],
  "capture": {"rvictl": "optional"}
}
```

`mode: "deny-all"` permits no radio-interface traffic during the candidate
window. `interfaces` are prefix-scoped (`pdp_ip` covers `pdp_ip0`, `pdp_ip1`,
…); loopback/tunnel/bridge prefixes are rejected outright. `allowlist` is the
future `{host, port}` escape for apps with required fixed endpoints.
`noiseFloorBytes` is the measured idle delta of this specific device (see §3) —
recorded at qualification time, not hardcoded.

### 2. Device-side counters (primary, unprivileged)

The XCTest helper samples `getifaddrs(3)` `if_data` byte counters and returns
them as `networkEvidence` (`schema: ios-network-counters`) on two existing
journaled boundaries — no new command was added:

- **start sample**: the `/activate` response — taken before candidate code
  runs.
- **end sample**: the `authority_cleanup` ack inside helper `shutdown` — after
  the candidate is terminated.

The CoreDevice tunnel rides the USB path, so control traffic is not counted.
Both samples are journaled as `networkEvidenceDigest` on the control ack and
surfaced through `IOSG4Provider.network_window`. `IOSTrustedMobileAdapter`
evaluates `ios_egress.measurement_evidence` after `service.replay()` returns:

- `delta > noiseFloorBytes` → `MobileFailureObservation` with
  `egress_violation`; the replay comparison is not trusted because verdict
  inputs may have been externally influenced.
- Missing or malformed counter evidence (helper omitted it, counters
  unreadable, bad schema) → fail-closed: activation/cleanup rejects the
  response and the run ends `mobile_replay_failed`/quarantined — never
  silently clean.
- Journaled `authority_cleanup` acks cannot replay without
  `networkEvidenceDigest` when an egress policy is bound.

### 3. Host-side capture (optional, privileged)

`rvictl -s <udid>` + pcap on `rvi0` during the candidate window gives
flow-level evidence — but BPF requires root or ChmodBPF, so it is an optional
attestation tier, not a gate:

- When available: `traffic.pcap` artifact + allowlist-diffed flow summary →
  `egressCapture: "clean" | "violated"` in the repair report.
- When unavailable: `egressCapture: "unavailable"` — degrades to counters,
  never blocks the run. (Same pattern as SDK-conditional checks elsewhere.)

### Enforcement tier (documented, not built)

Hard blocking exists only on the supervised path: a configuration profile with
`com.apple.proxy.http.global` pointed at a loopback blackhole, or per-app VPN
lockdown — both require device supervision/MDM. That is a **D5 acceptance
environment** prerequisite, not a product code gap. A dedicated egress-free
SSID/VLAN for the QA device is the equivalent physical control.

## Implementation mapping

| Surface | Change (implemented) |
|---|---|
| `reproof/ios_egress.py` | new module — policy validation, `network_counters` schema check, `counter_delta`, `measurement_evidence` |
| `generate-protected-config.py` | emits `egress-policy.json`; `egress` reference in `mobile-definition.json`; `egressPolicyDigest` in draft summary |
| `ios_mobile_inputs.py` / `ios_mobile_operation.py` / `ios_mobile_configuration.py` | `egress` field on `IOSMobileInputsConfig`; `egress_policy_digest` on `IOSMobileDefinition`; recovery config reconstructs it |
| `ios_mobile_xctest.py` / `ios_xctest_template.py` | `egressPolicyDigest` into `runtimeIdentity` + `REPRO_LIVE_EGRESS_POLICY_DIGEST` env (template whitelist) |
| helper source (`live-ios/Tests/LiveControlTests.swift`) | `getifaddrs`/`if_data` reader; `networkEvidence` on `/activate` and `authority_cleanup` acks — new helper build ⇒ IPA digest rotation |
| `ios_mobile_helper.py` | `networkEvidence` validation on activate/cleanup; `networkEvidenceDigest` on journaled acks; cached-replay consistency |
| `ios_mobile_g4.py` | captures start/end samples into `network_window`; missing evidence → `ios_g4_egress` |
| `repair_ios.py` / `repair_mobile.py` | `_egress_measurement` verdict; `egress_violation` reason; `egress_measurements` evidence retained per attempt |
| `run-issue-lifecycle.py` + `rvi_capture.py` | optional rvictl capture around candidate stage; `egressCapture`/`egressMeasurement` report stages |
| tests | `test_ios_egress.py` (policy/counter/delta/adapter verdicts); egress-bound helper tests in `test_ios_mobile_helper.py` |

## Open measurements

- **Idle noise floor**: QA-iPhone's radio counters drift from system services
  (push, time sync). `noiseFloorBytes` must come from a measured idle window
  on this device — collect during the next physical session before choosing a
  threshold. Expect the floor to be small on a dedicated QA device with iCloud
  signed out, but it is a *measurement*, not an assumption.
- **Wi-Fi-off acceptance mode**: whether `devicectl` control survives the
  device radios being off is unverified — if it does, operator-guided radio
  silence is a zero-code hard mode worth documenting in the runbook.
