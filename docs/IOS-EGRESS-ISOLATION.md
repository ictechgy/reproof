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
  "kind": "ios-egress-policy-v1",
  "schemaVersion": 1,
  "mode": "deny-all",
  "allowedFlows": [],
  "noiseFloorBytes": 0
}
```

`mode: "deny-all"` permits no radio-interface traffic during the candidate
window. `allowedFlows` is the future allowlist (`{host, port, protocol}`) for
apps with required fixed endpoints. `noiseFloorBytes` is the measured idle
delta of this specific device (see §3) — recorded at qualification time, not
hardcoded.

### 2. Device-side counters (primary, unprivileged)

The helper host already owns a command channel inside the XCTest session. Add
a `network-counters` command that returns `getifaddrs(3)` `if_data` byte
counters for the radio interfaces:

- `en0` (Wi-Fi), `pdp_ip0`+ (cellular) — read at candidate-window open and
  close; the CoreDevice tunnel rides the USB path, so control traffic is not
  counted.
- The runtime receipt gains `networkDelta: {wifiBytes, cellularBytes}`.
- Verdict rule (fail-closed, same shape as `mobile_quarantined`):
  - `delta > noiseFloorBytes` → candidate is **egress-violated** — report
    `failed` + new reason `egress_violation`; the replay comparison is not
    trusted because verdict inputs may have been externally influenced.
  - Counter read failure (helper error, missing interfaces) → `unknown` →
    quarantine, never silently clean.

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

| Surface | Change |
|---|---|
| `generate-protected-config.py` | emit `egress-policy.json`, register digest in `mobile-definition.json` |
| `ios_mobile_native.py` / xctest session | call `network-counters` helper command at candidate-window boundaries; carry `networkDelta` into receipts |
| helper source (`live-ios` host app) | `getifaddrs` reader + `network-counters` command — new helper build ⇒ IPA digest rotation (same flow as r68) |
| `repair_mobile.py` | verdict rule: `networkDelta` over floor ⇒ `egress_violation` reason |
| `run-issue-lifecycle.py` | optional rvictl capture wrapper around candidate stage; `egressCapture` field in report |
| tests | counter-delta verdict unit tests; policy schema validation; capture-unavailable degradation |

## Open measurements

- **Idle noise floor**: QA-iPhone's radio counters drift from system services
  (push, time sync). `noiseFloorBytes` must come from a measured idle window
  on this device — collect during the next physical session before choosing a
  threshold. Expect the floor to be small on a dedicated QA device with iCloud
  signed out, but it is a *measurement*, not an assumption.
- **Wi-Fi-off acceptance mode**: whether `devicectl` control survives the
  device radios being off is unverified — if it does, operator-guided radio
  silence is a zero-code hard mode worth documenting in the runbook.
