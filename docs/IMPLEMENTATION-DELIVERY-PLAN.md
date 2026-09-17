# QA 기록·재현·기기 팜 구현 계획

2026-09-11 · 실행 기준

QA의 한 번의 조작을 영상, 실행 입력, 관찰 로그, 실제 시작 조건으로 남기고, 승인된 같은 시나리오로 원본 결함과 수정 후보를 비교한다. 먼저 기록·재현과 원격 호스트 연결을 사용할 수 있게 만들고, 일반 AI 수정은 검증된 실행 경로가 준비된 경우에 활성화한다.

검토 상태: Planner → Architect → Critic 3회 검토를 수행했다. 마지막 판정은 ITERATE였으며 합의 승인을 얻었다고 표시하지 않는다. 아래의 목표별 보완 사항은 마지막 검토의 필수 지적을 실행·검증 기준에 직접 반영한 메인 에이전트의 수정안이다. 원래 검토문은 artifacts/qa-delivery/review-architect.md와 review-critic.md에 보존한다. 구현 중 독립 코드 검토와 QA에서 이 항목들을 다시 검사한다.

완료 구분: 로컬 소프트웨어 검증, 실제 Mac 영상 인코딩·브라우저 재생, 소유한 네이티브 환경 검증, 격리 실행 환경 검증, 실제 두 Mac·회사 QA 검증을 각각 보고한다. 회사 앱/QA/fixture/두 번째 Mac/AI 정책은 아직 제공되지 않았다. 사본과 체크섬을 확보했으며 현재 기존 Python 테스트 294개가 통과했다.

## 현재 실행 결과 — 2026-09-12

G0–G7 공통 기능, G8a/G8b 실행 소프트웨어와 G9의 일반 수정 제안·보호 검증 조합을 구현했다. G9 정식 92개와 현재 G7 61개·G8b 56개가 통과했다. 현재 고유 검사 1,026개 중 998개를 새로 실행해 통과했고, 입력이 바뀌지 않은 G8a 25개를 재사용했다. 고정 Android 의존성 검사 3개는 차단 상태다. 결과는 `artifacts/qa-delivery/g9-parent-covered-tests.json`에 있다.

[G9 운영 문서](PROJECT-REPAIR.md)에 화면·API·CLI·소스/AI 정책·실행기 등록과 보존을 정리했다. 실제 회사 앱, VM, 서명, 기기 격리 provider, 회사 독립 관찰, AI 전송 정책과 두 Mac 입력이 없어 실제 환경 acceptance는 미완료다. 보호 경로의 통과 증거는 명시적인 VM/기기/서명 대역을 사용한 소프트웨어 검사다. 기본 JSON 구성은 제안만 제공하며, 업로드한 JSON이나 성공 보고서가 실행 권한을 발급하지 않는다.

## 최종 목표별 보완 사항 — 아래 원안보다 우선

- **G0 계약:** defect와 expected 두 술어, 시간창, 관찰 범위·신선도, 반복 횟수를 함께 고정한다. 원본은 (true,false), 후보는 (false,true)만 성공이다. (true,true), (false,false), unknown, 잘린 관찰, 조건 불일치는 성공이 아니다. 원본/후보 각각 기본 3회이며 총 시도 예산은 시작 전에 고정한다. 실패·무효 회차를 지우고 유리한 회차로 대체하지 않는다. 순간적으로 기대 상태를 지나간 경우도 고정된 안정성/관찰 계약에 따라 판정한다.
- **G0/G2/G4 불변 객체:** 원본 recording은 stop barrier의 마지막 admitted sequence와 당시의 receipts/unknown을 봉인한다. 이후 늦게 도착한 ack, 상태 해소, 종료·cleanup은 recording digest를 참조하는 append-only lifecycle receipt로 남긴다. package manifest는 이 불변 객체들을 선택한 별도 버전이다. 늦은 receipt가 원본을 덮어쓰거나 불확실했던 이력을 삭제할 수 없다.
- **G1a 권한 코어:** 한 HostAuthority가 SQLite journal, 부모 grant, 단조 증가 renewal sequence, replay watermark, canonical device lock을 관리한다. clock_sync의 보수적인 시간 구간 변환은 G1a에 구현해 native deadline 의존성을 먼저 해결한다. native grant deadline은 현재 유효한 부모 coordinator grant의 보수적 로컬 deadline 이하이다. 수신 시점으로 TTL을 다시 시작하지 않는다. coordinator 단절 후 host가 독자 갱신할 수 없다. dedup cache에서 지워진 operation도 watermark 이하이면 실행하지 않는다.
- **G1b 네이티브 연결:** Lab/legacy API/replay/repair/worker를 같은 HostAuthority의 호환 어댑터로 연결한다. 설치·실행·reset·부수효과가 있는 수집도 같은 권한을 거친다. input dequeue뿐 아니라 각 injection 직전에 native incarnation/deadline/sequence를 확인한다. 이전 v1 바이너리는 shared worker 서비스와 동시에 제어하지 않는 offline cutover를 요구한다. 동일 OS 서비스 계정과 기존 canonical lock namespace를 유지하고, 이전 controller를 종료·배제하지 못하면 shared mode를 시작하지 않는다. code/store/helper/protocol 호환표와 unresolved native ownership 시 rollback 거부를 검증한다.
- **G4 fixture:** 신뢰된 로컬 Python 등록 객체/명시적 관리자 CLI만 recipe를 등록한다. G5 전에는 loopback 단일 소유자 모드만 가능하다. adapter는 remote fencing, terminal operation status, idempotency retention을 선언한다. key는 allocation generation+operation identity+payload digest에 바인딩한다. 원격 fencing 또는 모든 이전 효과의 terminal 증거가 없는 allocation은 cleanup 응답만으로 재사용하지 않는다. 지연된 prepare가 cleanup 이후 완료되는 사례를 검증한다.
- **G5 권한 수명:** 브라우저 인증 문맥을 재검증해도 해당 세션의 만료·취소 제약을 보존한다. 프레임/종료 레코드 전송, 비동기 작업, worker의 실제 provider 효과 직전에 다시 확인한다. 요청 본문 대기 중 취소된 host가 이전 검사 결과로 입력할 수 없어야 한다. 과거 원본은 당시 등록된 project/policy로 읽고, 새 실행에는 현재 revision을 요구한다. 과거 세션 종료·작업 취소와 G1/G4의 확정된 정리는 유지한다.
- **G2/G7 capture·보관:** 과거의 정상 화면 관찰로 이후 픽셀을 허용하지 않는다. sample과 직접 연결된 classification을 제공할 수 없는 provider는 명시적으로 승인된 test-data capture 모드만 허용하거나 sample을 억제한다. 수집 전에 그 모드를 신뢰된 프로젝트 revision에 고정한다. spools, encoder 입력, 미완료 파일, thumbnails, exports, AI derivatives에 retention/tombstone을 적용하고 active run/export가 pin한 객체는 삭제하지 않는다. access revoke는 이후 요청을 막으며 이미 내려받은 자료를 회수하지 못한다.
- **G8a 경로·준비 검사:** read-only doctor는 전제조건의 존재만 확인한다. 지원 경로는 sealed source → 네트워크가 차단된 disposable macOS guest에서 고정 recipe 빌드 → host의 bounded artifact validator → 필요한 경우 host의 고정 codesign 전용 단계 → 승인된 device provider 설치 → host trusted scenario interpreter/별도 read-only validation process의 외부 관찰 → 종료·fixture/device cleanup이다. signing은 승인된 identity/entitlements만 사용하고 후보 스크립트를 실행하지 않으며 pre/post artifact 관계를 기록한다. signing 자격증명은 AI·guest·후보 소스에 전달하지 않는다. 입력이 없으면 physical iPhone candidate 경로만 막는다.
- **G8a/G8b/G9 검증 권한:** 각 필수 regression은 project revision에서 고정한 외부 관찰 명세/UI·backend predicate 또는 candidate와 분리된 고정 runner의 독립 증거를 요구한다. 후보 프로세스의 XML/JSON은 supplemental이며 그것만으로 verified가 될 수 없다. 별도 VM 통합 테스트는 실제 boot/네트워크·파일·프로세스 제한/종료 증거를 확보해야 통과한다. VM image·offline toolchain 미제공 상태에서는 구현 가능한 protocol·제안·실패 경로를 계속 만들고 실제 실행은 차단한다.
- **검사 registry:** 각 항목은 실제 executable command, 필요한 환경/effects, 기대 evidence, software gate 또는 environment gate 구분을 가진다. 선택된 검사가 없거나 필수 검사 누락이면 실패한다. --effects는 검사 선택 선언이며 sandbox라는 뜻이 아니다. 미래 검사는 존재하지 않는 성공 근거로 등록하지 않는다.

최종 순서는 G0 → G8a 준비/실행 경로 → G1a → G1b → G2 → G3 → G4 → G5 → G6 → G7와 기록/재현 검증 → G8b 실제 격리 실행 → G9 보호 수정/최종 검증이다. G8b 환경이 없어도 G9의 제안·검증 거부·입력 보호 경로는 구현하고 검사한다. 이때 G8b와 실제 verified acceptance는 blocked로 유지한다.

## 검토 원안

The revised plan should deliver **record/replay first, then protected AI repair**. Native dispatch fencing, immutable evidence provenance, observation coverage, and runtime containment are implementation requirements in the numbered goals below—not deferred operational notes.

This is a read-only planning result. No files changed, tests ran, devices were operated, credentials were read, or network services were accessed. It addresses the supplied critic feedback but has not received a new independent review.

**Scope and repository basis**

Ship a small self-hosted release: one coordinator, registered Mac workers, project-scoped device access, durable issue packages, prepared replay, browser video playback, and a separately qualified repair path. Use existing ADB/UiAutomation/XCTest providers.

Inspected facts:

| Existing path | Relevant finding |
|---|---|
| `reproloop/live/model.py`, `recordings.py` | Recording starts with `provider-reset` or `unknown`; schema requires `frame-references-only`. Events are persisted when frozen rather than through a durable admission journal. |
| `reproloop/live/providers.py` | Simulator dispatch creates a new command ID, losing the originating operation identity. |
| `live-ios/Tests/LiveControlTests.swift` | Physical bridge dequeues work without an incarnation/deadline check. |
| `android/live/src/main/java/io/reproloop/live/LiveInstrumentation.kt` | Native input queue and injection paths need authority checks. Observations use approved IDs and bounded traversal. |
| `reproloop/storage.py` | `Lease` uses kernel file locks in a per-user temporary directory. Existing Android and iOS providers already use these locks. |
| `reproloop/live/iphone.py` | Signed application validation explicitly requires the sample bundle; constructor also restricts fixtures to sample cases. |
| `reproloop/android_profile.py` | Existing trusted profile separation is useful, but fixture inputs must be empty and several contracts remain sample-shaped. |
| `reproloop/live/worker.py`, `worker_cli.py` | Authenticated worker transport exists; configuration constructs a static inventory and lacks physical iPhone wiring and durable artifact transfer. |
| `reproloop/live/server.py`, `live-web/` | Existing browser server is single-user and loopback-only. Browser assets and Node tests are in `live-web/`. |
| `reproloop/orchestrator.py`, `replay.py` | Repair protects selected inputs but permits only a numeric-expression edit; build, regression, and candidate installation are not an end-to-end containment boundary. |

This Mac is arm64; Swift and Xcode are available. Neither ffmpeg nor ffprobe was found on PATH. The hypervisor support query was denied, so VM feasibility remains unqualified.

Assumptions and constraints:

- This workspace remains non-Git. Preserve existing source and artifacts; use manifests and separate output directories for change tracking and recovery.
- Company source/builds, actual issues, backend fixture contracts, second-Mac access, and AI-transfer policy remain external inputs.
- Future implementation may use authorized synthetic tests and owned disposable environments. Commands below are planned deliverables, not commands executed during planning.
- No automatic logging promise for all Native/Compose/SwiftUI/Flutter/RN applications. Providers declare supported observations; application-specific adapters are optional.
- Non-goals: high availability, public SaaS tenancy, arbitrary workflow scripting, general Appium compatibility, automatic backend cloning, unrestricted physical-touch recording, and universal high-FPS capture.

**Architecture choices**

| Decision | Alternatives and tradeoff | Choice |
|---|---|---|
| Playable video | A small AVFoundation encoder uses the installed Apple toolchain; ffmpeg offers mature codecs but adds an unavailable executable and distribution dependency. | AVFoundation helper now; versioned encoder interface leaves ffmpeg optional later. Encode actual captured samples with their timing. Do not advertise high-FPS native capture. |
| Durable state | Extending JSON files is simple but complicates atomic device ownership, allocations, and storage reservations. SQLite provides transactions without another service. | SQLite per coordinator/host, plus immutable artifact files. No shared SQLite over a network filesystem. |
| Configuration execution | Imported shell commands or URLs are flexible but let recordings expand authority. A registered typed operation catalog is narrower and auditable. | Trusted project registration defines recipes and endpoints; recordings reference IDs and typed variables only. |
| Repair containment | Source copies and process groups help integrity/cancellation but are not a sandbox. A qualified VM adds substantial setup but can isolate build and desktop execution. | Qualified VM backend; no unrestricted host fallback. Mobile execution has a separate qualification gate. |

The coordinator owns authorization and scheduling. Each host owns physical dispatch, local locks, recording journals, and artifact production. Loss of coordinator connectivity must not permit indefinite execution.

**G0 — Freeze contracts and implement their validators**

Ownership: add `reproloop/contracts/` with `project.py`, `evidence.py`, `scenario.py`, `observation.py`, `execution.py`, and `versions.py`; add `docs/RELEASE-CONTRACTS.md`. Existing legacy validators remain unchanged.

Define these distinct objects:

1. **Trusted project revision:** project/trust-group identity; application identities; accepted build provenance; variable declarations; fixture recipes; approved observation fields; evidence policy; editable paths; build/regression recipes; execution-class requirements. Registration is an authenticated administrative operation.
2. **Original evidence record:** immutable session facts, admitted commands and receipts, actual preparation receipts, observation/media references, identity, clock mappings, interruptions, and unknowns.
3. **Approved executable specification:** references the original digest; maps actions to original event IDs; adds explicitly authored waits, bindings, fixture requirements, and assertions with author/revision provenance.
4. **Qualification record:** binds the approved specification, project revision, fixture-equivalence rules, observation contract, original build, runtime environment, and validation recipes.
5. **Candidate run:** permits an explicitly defined source/build identity substitution. All other required invariants remain frozen.

Bind project/application/build identity before admitting the first input to a new original-record session. Obtain preparation receipts before that session’s inputs if preparation was actually performed. Later fixture authoring cannot backfill historical facts.

Specification changes produce a new revision and invalidate previous qualification. Imported approvals remain provenance; they do not automatically confer local execution authority.

Observation envelopes must include:

- Observation ID, provider/native incarnation, application identity.
- Capture start/end interval and clock mapping uncertainty.
- Root/window scope, approved target set, supported properties.
- Traversal limits, truncation, collection errors, and completeness.
- Sampling schedule or continuous event-coverage declaration.

Use three predicate classes: snapshot predicates, predicates at declared sampling points, and continuous predicates. Missing or truncated coverage produces `unknown` where coverage is required. Sampled snapshots cannot prove continuous absence. Freeze provider-appropriate freshness parameters; do not introduce a universal 500 ms limit.

Evidence policy covers pixels, text, accessibility values, logs, and fixture receipts—not just input parameterization. It defines collection permission, restricted-screen behavior, retention, viewing/export rights, and AI eligibility. Unknown sensitive-screen status must use the project’s declared fail-closed behavior. Redacted derivatives have separate hashes and provenance; originals remain potentially sensitive.

Devices default to one project or explicit trust group. Reassignment requires a sanitation receipt stating residual limitations. Fixtures declare exclusive allocation or a reviewed concurrency-safe contract.

Define separate execution classes:

- Build guest.
- Desktop/host-native candidate runtime guest.
- Mobile candidate runtime on an approved device environment.

Each qualification identifies enforceable controls and operator attestations. An attestation cannot satisfy a requirement that calls for technical enforcement.

Acceptance:

- Post-recording fixture attachment does not establish original-state equivalence.
- Unauthorized project/specification changes fail; authorized candidate build substitution succeeds.
- Unsupported/truncated observations return unknown.
- A slower provider passes its approved freshness contract.
- Imported data cannot register executables, endpoints, plugins, or policy.
- Legacy files are accepted by their original validators without rewriting their bytes.

Add `tests/test_release_contracts.py` and schema examples under `tests/fixtures/release/`.

**Early G8 prerequisite checkpoint — immediately after G0**

Add `scripts/repair-backend-doctor.py`. Inspect only explicitly selected, non-secret prerequisites: virtualization support, guest-image metadata, resource availability, offline toolchain manifests, and required transport support.

The implementation checkpoint must determine whether an owned macOS guest can be provisioned, booted, isolated, and controlled on this Mac. Xcode installation alone is insufficient.

Record one of:

- `prerequisites-qualified`: proceed with G8 implementation and actual backend testing.
- `prerequisites-missing`: enumerate missing image/toolchain/authorization inputs; continue G1–G7.
- `backend-unqualified`: attempted qualification failed; repair execution remains disabled.

Do not download an image, install tools, or access signing material implicitly. This checkpoint is not VM acceptance.

**G1 — Durable host authority through native injection**

Ownership: add `reproloop/live/authority.py` and `state_store.py`; modify `storage.py`, `live/model.py`, `providers.py`, `android_live.py`, `iphone.py`, `worker.py`, and both native helpers. Audit lock callers in `device.py`, `ios_device.py`, `ios_runner.py`, and `live/android.py`.

One operation envelope survives every hop:

`operationId`, payload digest, project/session/controller identity, ownership generation, host/native incarnation, bounded deadline, and protocol version.

Requirements:

- Journal intent durably before dispatch. Persist acknowledgements separately.
- Preserve operation ID through Simulator, Android, physical iPhone, and worker bridges.
- Reject reused IDs with different payloads.
- Check authority and conservative native-local deadline immediately before injection, not merely at HTTP admission or dequeue.
- For multi-step gestures, check each dispatch boundary. Cancel active pointers through a narrowly defined cleanup path. An uninterruptible XCTest call keeps the device unavailable until its completion or termination is established.
- Use bounded native authority grants renewed by the host. Delayed renewals cannot extend expired ownership.
- Late acknowledgements are historical evidence; they cannot complete a new incarnation’s operation.
- Never retry uncertain input automatically.

Reuse kernel locks, but canonicalize physical identity before acquiring them. All supported worker processes on a host use one service account and authority root; configuration aliases and alternate output directories cannot create separate device locks. Refuse unsupported multi-account control rather than claiming per-user locks coordinate it.

On restart, quarantine unfinished ownership. Reconciliation requires evidence of the prior helper’s identity, stop/exit status, queued/in-flight operation disposition, active-pointer cleanup, and a fresh helper handshake. A vanished host process or released file lock alone does not prove the native helper stopped. If disposition remains unknowable, retain the unknown outcome and require an approved recovery procedure before reuse.

Compatibility and migration:

- Negotiate coordinator/worker/helper versions before device mutation.
- Introduce an explicit storage format and minimum-reader/writer version.
- Reject unsupported storage versions before starting services.
- Keep new storage outside legacy output roots.
- Add legacy adoption records binding existing byte digests and their limited semantics.

Acceptance: expiry while queued; late acknowledgement after reconnect; restart with queued work; duplicate physical-device aliases across processes; helper protocol mismatch before installation/input; rollback with unresolved ownership.

Effects inventory includes Python subprocess tests, Swift compilation, Android compilation, and later owned native execution. Native compilation is part of G1 completion.

**G2 — Durable recording, privacy enforcement, and storage admission**

Ownership: add `live/evidence_store.py`, `recording_session.py`, `disk_budget.py`, and `clock_sync.py`; integrate through `model.py`, `media.py`, and provider/native timestamp fields.

Recording lifecycle:

`preparing → recording → finalizing → frozen-complete | frozen-incomplete`

Recovery converts unfinished sessions to interrupted evidence. No restart automatically resumes recording, fixture work, or input.

Journal:

- Identity and actual preparation receipts.
- Input admission, dispatch, acknowledgement, rejection, and uncertainty.
- Frame acquisition/queue/encoder transitions.
- Observation scope and capture failures.
- Lifecycle, ownership changes, privacy suppression, and storage exhaustion.

An admitted input whose receipt is lost remains unknown; it does not disappear from the original record.

Clock protocol:

- Separate coordinator, host, native-provider monotonic clocks and wall-clock display time.
- Perform timestamped round-trip synchronization exchanges.
- Represent offset as a conservative interval including transit uncertainty.
- Accumulate approved drift bounds between exchanges.
- Invalidate mappings on sleep, reboot, native restart, monotonic discontinuity, or excessive uncertainty.
- Record segment-local presentation timestamps independently of wall time.

Privacy is enforced before persistence and streaming. Where reliable screen classification is unavailable, do not promise automatic pixel redaction: require an approved capture mode or suppress capture with an explicit gap.

Storage admission is host-wide and transactional. Reserve capacity for journal growth, frame spool, active encoding, segment finalization, uploads, and freeze metadata across concurrent sessions. Protect journal headroom from media consumption; external disk pressure still triggers bounded shutdown and an incomplete result. If admission cannot be journaled, reject new input.

Acceptance: crash at each persistence boundary; concurrent reservations; external disk exhaustion; secret text/pixels following declared policy; truncated journal recovery; wall-clock changes and sleep invalidation. Existing artifacts remain unchanged.

Add `tests/test_evidence_store.py`, `test_recording_recovery.py`, and `test_clock_sync.py`.

**G3 — AVFoundation video and event mapping**

Ownership: add `native/macos-video/Package.swift`, `Sources/ReproVideo/main.swift`, `reproloop/live/video.py`, and `scripts/verify-video.py`.

Implement a small bounded encoder protocol:

- H.264 MP4 segments using AVFoundation.
- Input dimensions/bytes/timestamps validated before decode and append.
- Segment rotation on configured duration, geometry change, or clock discontinuity.
- Segment-local PTS mapping with explicit first/last captured frame times.
- Manifest entries containing checksum, dimensions, source sampling mode, frame count, capture intervals, and uncertainty.

Segment lifecycle:

`open → sealing → durable`, with `failed` or `unrecoverable` alternatives.

An encoder-accepted frame is not durable until the segment is finalized, flushed, and atomically published. Keep separate evidence for:

- Captured frames dropped before encoding.
- Encoder-accepted frames lost with an unfinished segment.
- Periods where no frame was acquired.

Use a small fixed finalizer/backpressure limit. If it is exceeded, the recording policy either pauses input or records a gap. Stop-recording stops new admissions immediately; prolonged finalization cannot extend the original scenario. Bound finalization time and freeze incomplete evidence when necessary.

No blanket “two-second loss” guarantee: report the actual vulnerable segment interval, queued frames, and unknown capture interval separately.

Actual evidence required on this Mac:

- Compile and execute the helper.
- Encode generated timestamped frames with motion.
- Independently decode through AVFoundation, check frame count/PTS/content markers, and retain the playable MP4.
- Exercise irregular cadence, rotation, killed encoder, prolonged finalization, and disk-full behavior.
- Verify browser decoding in G7.

This proves real encoding of supplied frames. It does not prove high-FPS iPhone capture.

Add `tests/test_video_protocol.py`; place native encoder acceptance in `scripts/verify-video.py`.

**G4 — Prepared original recording and approved replay**

Ownership: add `reproloop/fixtures.py`, `scenario_runner.py`, `qualification.py`, and `live/issue_sessions.py`; integrate with `live/jobs.py`, `model.py`, and the new project contracts. Keep legacy `replay.py` behavior behind its existing API.

Implement a bounded scenario interpreter:

- Registered tap/text/swipe/system actions.
- Approved locator or explicit geometry requirements.
- Typed variable references; secret values resolved at execution without journal disclosure.
- Bounded waits.
- Immutable snapshot/sampling/continuous assertions.
- Registered backend observations and fixture operations.

No `eval`, imported shell snippets, arbitrary URLs, or import-by-string plugin registration.

Prepared recording sequence:

1. Validate project/application/build and evidence policy.
2. Reserve device and fixture allocation.
3. Execute registered preparation and start-state checks.
4. Persist actual receipts and freeze their relation to the original session.
5. Admit QA input.
6. Stop and freeze evidence.
7. Perform cleanup; retain exclusion until cleanup is confirmed.

For an unprepared session, preserve unknown initial conditions. A subsequently approved specification may support useful replay, but it cannot claim proven equivalence to unknown historical conditions.

Fixture allocation uses durable ownership generations and idempotency keys. On timeout, inspect a registered operation’s status rather than blindly repeat a side effect. Unknown cleanup quarantines the allocation independently of device status.

Verdicts:

- `injected`: provider acknowledgement of injection.
- `observed`: supported evidence was collected.
- `reproduced`: qualified original-build replay satisfies the frozen defect predicate with required preparation and coverage.
- `verified`: reserved for G9 candidate validation, including trusted regressions and cleanup.
- `unknown`, `failed`, `cancelled`, and `quarantined` remain distinct.

Acceptance includes a stateful local fixture service, two devices competing for one exclusive allocation, cleanup failure, changed specification, unsupported continuous absence, and authorized candidate identity substitution. Such tests validate contracts, not a company backend.

Add `tests/test_fixture_allocations.py`, `test_scenario_runner.py`,
`test_qualification.py`, and `test_issue_sessions.py`.

The implemented local G4 API and its explicit non-company limitations are
described in [Prepared recording and frozen scenario replay](PREPARED-RECORDING.md).

**G5 — Project access, enrollment, and migration boundaries**

Ownership: add `live/access.py`, `enrollment.py`, and `configuration.py`; integrate `server.py`, `cli.py`, `worker_cli.py`, and `state_store.py`.

Ship one central coordinator with explicit administrator-provisioned identities and project membership. Separate viewer, operator, maintainer, and administrator capabilities.

- Authenticate every issue, media range, session, export, import, fixture, and repair operation.
- Apply project membership and trust-group boundaries.
- Require TLS for non-loopback transport.
- Use scoped host credentials, one-time enrollment, revocation, and bounded token lifetimes.
- Keep secrets out of CLI arguments, examples, logs, and exported packages.
- Preserve same-origin/CSRF protections.
- Keep the existing loopback legacy console mode clearly separate from shared deployment.

Provide non-secret configuration examples and administrative CLI operations for project registration, host enrollment, device assignment, and sanitation receipt attachment.

Migration acceptance:

- Explicit legacy adoption preserves bytes and records original format/meaning.
- Existing legacy `verified` remains a legacy result; it is never promoted to company verification.
- Unknown storage versions fail startup.
- Supported older writers refuse newer stores.
- Pre-upgrade binaries cannot be retroactively taught a version check: isolate new stores under a service-owned namespace inaccessible to those legacy writers.
- Restore/import never restores live grants or resumes fixture operations.

Add `tests/test_project_access.py`, `test_enrollment.py`, and `test_release_migrations.py`.

**G6 — Physical workers, application profiles, and artifact transport**

Ownership: `worker.py`, `worker_cli.py`, `providers.py`, `android_live.py`, `iphone.py`; add `live/artifact_transfer.py` and `reproloop/ios_profile.py`. Native helper changes remain under one provider-integration owner.

General application profiles must identify:

- Platform/package or bundle ID and launch target.
- Accepted application artifact/build provenance.
- Helper/protocol requirements.
- Locator and observation capabilities.
- Approved launch/preparation bindings.
- Optional app-log/capture adapter and version.

Remove sample-only iPhone assumptions from the general provider path. Keep sample fixtures as legacy adapters. Do not require arbitrary applications to contain the sample SDK or Repro Loop-specific build metadata.

Verify selected artifact identity before install and the strongest available installed/launch identity evidence afterward. Where a platform cannot supply required identity proof, qualification fails rather than substituting an assertion.

Android workers must accept explicit project/application profiles instead of silently selecting sample defaults.

Transport:

- Authenticated, resumable, bounded chunks for video, captures, app logs, and manifests.
- Server-generated artifact IDs; no arbitrary filesystem paths.
- Per-object/project/host size quotas and disk reservations.
- Digest verification and atomic publication before artifacts become available.
- Retries allowed for idempotent chunks; never for uncertain native input.
- Persist upload state and reconcile after reconnect.

Inventory refresh applies only to enrolled hosts. Authenticate host incarnation and generation; reconcile additions/removals without freeing active or uncertain devices. Duplicate device registrations across aliases fail. Reconnect requires G1 reconciliation.

Acceptance:

- Non-sample iPhone bundle/profile with optional logs absent.
- Wrong bundle, artifact change, unsupported observation, and signing-product mismatch.
- Explicit non-sample Android profile.
- Interrupted/repeated/corrupted uploads and cross-project artifact requests.
- Duplicate physical registration across worker processes.
- Coordinator/worker/helper mismatch fails before mutation.
- Same-Mac subprocess integration is reported as such.

Actual physical iPhone acceptance requires authorized signed test products and an owned device. Two-Mac acceptance remains pending until those machines are available.

Add `tests/test_worker_profiles.py`, `test_worker_artifacts.py`, and `test_worker_recovery.py`; extend existing worker/provider tests.

G6 software evidence (2026-09-12): `g6-release-parent-r4.json` records 82
required tests; `g6-parent-final-covered-tests.json` records 669 distinct tests
including all 587 previous accepted identities. Native helper compilation and
actual video encoding/decoding passed in the linked G1/G3 gates. The
[worker runtime guide](WORKER-RUNTIME.md) documents the current APIs and limits.
General iOS logs/accessibility are rejected until a compatible adapter exists;
the existing sample-only log collector is not advertised for arbitrary apps.
Physical-device and two-Mac deployment acceptance remain pending.

**G7 — Browser issue workflow and replayable packages**

Ownership: add `reproloop/issue_package.py` and `live-web/video.js`; modify `live-web/app.js`, `index.html`, `styles.css`, `server.py`, `client.py`, and `commands.py`.

Browser workflow:

- Select project/build/preparation before recording.
- Display preparation, policy, capture, and ownership status.
- Record once; stop; show finalization and incomplete reasons.
- Inspect original evidence separately from the approved specification.
- Play actual video with action markers, uncertainty, sample cadence, and interruptions.
- Approve a new executable specification through an authorized maintainer action.
- Replay and compare receipts, observations, predicates, and evidence.

Timeline behavior:

- Seeking within a gap displays a gap; it must not silently jump to another image.
- Do not hold the last frame across a known gap as if it were current.
- Show seek-position accuracy separately from captured-frame age.
- Hide unavailable media and show the reason rather than fabricate it.

Packages contain checksummed original evidence, specification revisions, actual receipts, video segments, observation coverage, and qualification provenance. They exclude secret variable values and executable policy registration.

Import uses bounded staging and rejects traversal, symlinks, duplicates, oversized expansion, malformed media, unknown required versions, and checksum mismatch. Imported content is inert until local project binding and execution approval. Validate trust separately from content hashes.

Acceptance: browser playback, action seeking, gap seeking, permission revocation, interrupted upload, export/import replay, unknown original state display, and legacy recording APIs.

Add `live-web/video.test.mjs`, `tests/test_issue_package.py`, and `scripts/qa-record-replay.py`.

G7 software evidence (2026-09-12): `g7-release-parent-r4.json` passed 61
registered checks. `g7-release-gate/20260912T074806Z-c5769014/result.json`
records the actual owned backend, coordinator process restart, two worker CLIs,
MP4 package round trip and three original replays on the second worker. Its
12 browser checks include a separately authored UI recording/replay, rotation,
action/gap seeks, stale callbacks and revocation. A separate actual browser
check covers catalog refresh after reservation release and unsaved edits.
Remote native acquisition remains unknown and the original remains incomplete.
[Issue workflow operations](ISSUE-WORKFLOW.md) documents the routes and CLI.
`g7-parent-covered-tests.json` accounts for all 878 current test identities:
875 passed, while three Android build/bytecode checks are blocked by missing
local Gradle 8.14.5, AGP 8.13.2 and ASM 9.8 dependencies. Restore those inputs
and rerun the recorded checks before full G7 acceptance. This environment
block does not authorize changing the pinned build or claiming a full pass.
Continue the independently implementable G8b protocol and G9 proposal/denial
work while retaining the blocked acceptance. Actual company/two-Mac and
protected execution acceptance remain pending.

**Record/replay acceptance gate**

G0–G7 must provide a coherent usable release before repair integration is required.

Demonstrate:

1. Prepared recording using registered application and fixture contracts.
2. Durable playable video plus actions, observations, identity, and receipts.
3. Freeze, service restart, export/import, browser playback, and approved replay.
4. Original defect reproduction with unchanged specification and valid coverage.
5. Worker loss, stale authority, cleanup failure, privacy suppression, and storage exhaustion without false success.
6. Existing capture/bundle APIs and legacy artifacts retain their meaning.

Report separately:

- Software/unit/subprocess integration passed.
- Real video encoding/playback passed on this Mac.
- Owned native provider runs passed or pending.
- Two-Mac physical farm acceptance pending.
- Company issue/state-equivalence acceptance pending.

VM absence cannot block this gate.

**G8 — Qualified build and runtime execution backend**

Ownership: add `reproloop/execution/{backend.py,protocol.py,qualification.py,artifacts.py}`, `native/macos-execution/`, `guest/reproloop_agent/`, `scripts/provision-repair-guest.py`, and `docs/REPAIR-EXECUTION.md`.

Implement one backend, not a provider marketplace: an Apple Virtualization-based macOS guest backend, subject to the early feasibility checkpoint.

Concrete deliverables:

- Versioned host/guest protocol and bounded authenticated transport.
- Image provisioning from explicitly supplied resources.
- Base-image and offline-toolchain manifests.
- Boot, readiness, health, and shutdown.
- Per-run writable disk clone/overlay.
- Immutable input transfer and bounded artifact extraction.
- No writable original-source or credential mounts.
- Default-denied guest networking; explicitly qualified connectivity where required.
- Cancellation, guest termination confirmation, overlay disposal, and recovery quarantine.

Separate containment classes:

| Class | Required boundary |
|---|---|
| Build | Candidate-influenced build scripts execute in the guest. Dependencies/toolchains are pinned and supplied offline. |
| Desktop runtime | Candidate processes remain in a disposable qualified guest. Never execute an extracted candidate on the Mac host. |
| Mobile runtime | Approved device/trust group, test accounts, backend scope, entitlements, network controls, and cleanup procedures. |

Guest isolation does not contain a mobile app installed afterward. Mobile qualification must account for Wi-Fi, cellular, VPN, background activity, accounts, keychain/shared storage, and backend side effects. Controls the available device environment cannot enforce cannot be marked enforced.

Candidate-generated XML, JSON, or “passed” files are untrusted. A trusted supervisor records run identity, recipe, input/artifact hashes, lifecycle, and termination. Trusted validation must execute outside candidate control; a MAC on a guest report alone does not make candidate-produced test results trustworthy.

For in-process regressions that cannot provide sufficient independent evidence against the declared adversarial model, mark them supplemental and block any qualification that requires stronger proof.

Acceptance: denied backend access, network escape attempts, forged reports, stale results, candidate child surviving parent exit, early termination, cancellation, dirty overlays, and retained mobile state. Missing termination or cleanup confirmation prevents `verified`.

Add `tests/test_execution_protocol.py`, `test_execution_qualification.py`, and `scripts/qa-execution-backend.py`. Protocol doubles do not pass actual VM acceptance.

Current G8b software result (2026-09-12): the concrete Apple Virtualization
bridge, guest launchers/agent, bounded authenticated protocol, resource sealing,
project-bound artifact admission, durable cancellation and native-record recovery
are implemented. G8b's 56 software tests and G8a's 25 current authority/prerequisite
tests passed. The actual native tools compiled and rejected host/invalid-resource
execution; this did not boot a guest. `artifacts/qa-delivery/g8b-environment-r1/result.json`
records `blocked-unqualified` because no owned environment was supplied. G8b
acceptance therefore remains blocked. See [Protected repair execution](REPAIR-EXECUTION.md).
G9 now implements proposal, immutable-input and protected-supervisor composition
under this environment limit. Its protocol doubles exercise the internal verdict
path; they do not enable a production environment or prove actual acceptance.

**G9 — General project-aware repair and protected verification**

Ownership: add `reproloop/project_repair.py` and `validation.py`; integrate `agents.py`, `live/repair_jobs.py`, and browser repair presentation. Preserve legacy orchestrators for their existing restricted use.

Sequence:

1. Load locally approved original evidence/specification and project revision.
2. Validate original source/build provenance.
3. Qualify the fixture, observation, and execution environment.
4. Reproduce the defect on the original build; default to three valid runs.
5. Freeze repair inputs, repetitions, validation recipes, and runtime qualification.
6. Create an isolated candidate from the approved source manifest.
7. Supply only policy-eligible source/evidence to the configured AI adapter.
8. Accept edits only within explicit product-file boundaries.
9. Build and run trusted configured regressions through G8.
10. Replay the unchanged approved scenario with the authorized candidate build substitution.
11. Confirm original integrity, candidate termination, fixture cleanup, and device sanitation.
12. Publish patch, build provenance, before/after evidence, and bounded verdict.

Candidate changes cannot alter fixtures, original records, assertions, trusted test harnesses, build/validation policy, or qualification. Reject symlink/path escapes, undeclared files, policy changes, and content changes outside the approved edit manifest.

No company AI transfer occurs without its policy. A local deterministic proposal adapter can exercise orchestration but must be reported as a test adapter, not real AI acceptance.

Cancellation is durable and terminal for verification. It revokes dispatch, stops the guest, stops the mobile candidate through the approved provider, and retains quarantines until cleanup is confirmed.

Acceptance: product-file fixes beyond numeric expressions, irrelevant patch, no-op patch, altered assertion, corrupted original, forged regression output, failed baseline, unauthorized candidate identity change, permitted build substitution, unavailable AI adapter, cancellation, and cleanup failure.

Add `tests/test_project_repair.py`, `test_trusted_validation.py`, and `scripts/qa-protected-repair.py`.

Current G9 software result: 92 required tests pass in
`artifacts/qa-delivery/g9-release-parent-r3.json`. A real isolated browser exercises
proposal generation, the composed verification flow, patch review, three candidate
results, narrow layout, late responses and credential revocation. Fixed local
signer/inspector and native mobile adapters remain operator integration points;
actual company acceptance is blocked, as recorded by
`g9-environment-r1/result.json`. See [Project repair](PROJECT-REPAIR.md).

**Verification commands and evidence matrix**

G0 must add `scripts/release-check.py` as a test-selection/reporting harness. The commands below are **new interfaces to implement**. They do not exist merely because this plan specifies them.

Each goal suite must reject missing required checks; report `pass`, `fail`, or `blocked` per check; and return nonzero if a required check failed or was blocked. `--effects` declares selected effects—it is not an enforcement sandbox. Tests must separately restrict operations to explicitly selected owned disposable resources.

Ordered commands, run during authorized implementation:

```bash
python3 scripts/release-check.py --goal G0 --effects filesystem
python3 scripts/repair-backend-doctor.py --read-only

python3 scripts/release-check.py --goal G1 --effects filesystem,process,loopback,native-compile
python3 scripts/release-check.py --goal G2 --effects filesystem,process,loopback
python3 scripts/release-check.py --goal G3 --effects filesystem,process,native-compile

python3 scripts/verify-video.py --backend avfoundation --cases all --output-new
python3 scripts/release-check.py --goal G4 --effects filesystem,process,loopback
python3 scripts/release-check.py --goal G5 --effects filesystem,process,loopback
python3 scripts/release-check.py --goal G6 --effects filesystem,process,loopback,native-compile
python3 scripts/release-check.py --goal G7 --effects filesystem,process,loopback,native-compile,browser

python3 scripts/qa-record-replay.py --environment synthetic-local --output-new artifacts/NEW_RECORD_REPLAY --browser
python3 -m unittest discover -s tests -v
node --test live-web/stream.test.mjs live-web/pointer.test.mjs live-web/video.test.mjs
```

Native and repair commands require explicit environment descriptors. Descriptors use public device aliases and non-secret policy references; output must not reveal raw device IDs.

```bash
python3 scripts/qa-record-replay.py --environment "$REPRO_OWNED_ENVIRONMENT" --output-new artifacts/NEW_NATIVE_RECORD_REPLAY

python3 scripts/release-check.py --goal G8 --effects filesystem,subprocess,native-compile
python3 scripts/qa-execution-backend.py --environment "$REPRO_GUEST_ENVIRONMENT" --output-new
python3 scripts/release-check.py --goal G9 --effects filesystem,process,loopback
python3 scripts/qa-protected-repair.py --environment "$REPRO_REPAIR_ENVIRONMENT" --output-new
```

After synthetic validation, run the same record/replay and repair harnesses against supplied two-Mac/company environment descriptors. Do not fabricate descriptors for unavailable infrastructure.

| Area | Unit | Integration | E2E evidence | Observability |
|---|---|---|---|---|
| Authority | Envelope/deadline/version validation | Competing processes, late acknowledgements, restart | Owned native queued-expiry and reconnect | Rejection reason, incarnation, unknown operations, quarantine |
| Original/specification | Digests, provenance, substitution rules | Prepare/record/approve/requalify | Same frozen specification before/after | Original digest, spec revision, qualification reason |
| Video/storage | PTS mapping, quota accounting | Crashes, concurrent reservations, finalizer stalls | Real encoding, decoding, browser gap seek | Frame cadence, queue loss, segment loss, gaps, reserved bytes |
| Fixtures/coverage | Predicate semantics and allocation rules | Exclusive contention, truncated/slower observations | Prepared replay with confirmed cleanup | Allocation state, coverage/freshness, unknown predicates |
| Access/workers | Permission and profile validation | Revocation, chunk resume, inventory reconciliation | Owned providers; separate two-Mac run | Denied scope, host generation, transfer integrity |
| Repair | Edit/validator protections | Forged reports, timeout, cancellation | Actual qualified guest and mobile environment | Frozen policy, trusted validation, termination and cleanup |

Audit logs contain opaque IDs, bounded reason codes, digests, and timing—not raw input, device IDs, credentials, or unrestricted observations.

**Three-scenario pre-mortem**

| Failure scenario | Prevention and detection | Recovery |
|---|---|---|
| A disconnected worker later executes an old gesture on a newly assigned device. | Native-local expiry/incarnation checks, canonical process locks, durable unknown operations, no automatic reassignment. | Reconcile helper and queue; confirm pointer/process cleanup; otherwise quarantine. |
| An issue looks reproducible, but its original account state was unknown or a transient failure occurred between snapshots. | Separate original evidence from authored specification; actual-only preparation receipts; coverage-aware predicates. | Preserve unknowns, invalidate qualification, obtain a newly prepared recording or stronger coverage. |
| A candidate fabricates passing reports or continues contacting a backend after cancellation. | Qualified guest/mobile boundaries, independent validation, restricted runtime networking, durable cancellation, cleanup-gated verdict. | Terminate and confirm; quarantine device/allocation/environment; retain failed evidence and issue no verified result. |

**Migration, rollback, and recovery**

Before each goal, implementation should record a source manifest and make a separate recovery copy of files it will change. Never overwrite existing artifacts or use Git commands in this workspace.

Storage migration is explicit and offline. Preserve legacy bytes; create adoption records and a separate new store. Incompatible peers fail before mutation. Backups contain evidence and configuration references, not reusable live grants.

Rollback stops new admissions, reconciles or quarantines active native work, preserves incomplete recordings and uploads, and switches to the previous compatible code/store pair. Never restore live leases, replay uncertain commands, or automatically resume fixture cleanup from a database snapshot. Unfinished cleanup remains a recovery task with explicit status inspection.

Media can be reindexed from immutable segment manifests; missing segments remain gaps. Failed uploads remain unpublished. Failed sanitation prevents reassignment.

**Delivery discipline and final acceptance**

Execute in this order:

`G0 → early G8 prerequisite assessment → G1 → G2 → G3 → G4 → G5 → G6 → G7 → record/replay gate → qualified G8 → G9 → repair gate`

After each goal, review its diff, run its scoped acceptance commands, and record changed files, evidence, failures, and external blockers. Continue to the next authorized goal when its dependencies pass; the plan is not a stopping point for the eventual implementation executor.

After G0, the standalone encoder and execution-backend work can proceed independently. Keep `model.py`, `server.py`, provider bridges, native helpers, and shared contract files single-owner; integrate their changes sequentially. No parallel worker should edit those shared files concurrently.

The final acceptance report must separate:

- Implemented and tested software capabilities.
- Actual video evidence from this Mac.
- Actual native/VM environments exercised.
- Same-Mac versus two-Mac evidence.
- Real AI use versus proposal doubles.
- Pending company source, issue, fixture/backend, AI policy, and infrastructure inputs.

Complete company acceptance requires supplied real Android/iPhone issues, reproducible preparation, another developer’s remote replay, and original-failing/candidate-passing evidence under unchanged approved criteria. Additional toy app cases cannot substitute for that acceptance.
