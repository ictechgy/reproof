# Handoff

_Last updated: 2026-09-16 by Devin (r53)_

## Goal

회사 QA 이슈를 영상·행동·시작 조건으로 기록하고 Android/iPhone에서 재현 → AI 수정 →
같은 승인 원본으로 보호 검증하는 제품을 완성한다. r51에서 실기기 전 개발 범위를 완료했고,
이번 r52 요청은 **“다음 단계부터 진행해줘”**(실환경 qualification 시작)였다.
사용자 승인: 물리 iPhone `QA-iPhone` 사용, 저장소 샘플 앱, Xcode/로컬 개발 프로파일 서명, 한 Mac만.
회사 앱·PKCS#12 서명 자료·VM·두 번째 Mac은 제공되지 않았다.

## r52 결과 (2026-09-16, 실기기 측정 + qualification runner)

- 기록 위치: `artifacts/product-delivery/d4-ios-device-qualification-r1/`
  ([acceptance.json](artifacts/product-delivery/d4-ios-device-qualification-r1/acceptance.json)).
- **QA-iPhone 샘플 흐름 실측**: 로컬 프로파일 서명 빌드 → 설치(signatureVerified) → 기록 → 원본 결함 3/3 →
  오프라인 패치 후보 정상 3/3, 회귀 1 통과·0 skip. 모두 `executionEnvironment: physical-iphone`.
- **잠긴 기기 경계 실측**: `passcodeRequired: true` 상태에서 XCTest가 180초 예산을 초과해 `runValid: false`.
  잠금 해제 후 정상. 이 관측이 `device-boundary`의 `unlocked` 조건 근거다.
- **helper 경계 exercise** ([device-boundary-exercise.json](artifacts/product-delivery/d4-ios-device-qualification-r1/device-boundary-exercise.json)):
  tunnel ULA listen, 토큰 없음/오류 401, incarnation 일치, `/activate`·`/retire`, 다른 incarnation·retire 후 명령 거절,
  XCTest exit 0(1 pass/0 skip), helper 프로세스 부재, 앱 삭제 확인. 이전 세션이 남긴
  `io.reproloop.*` 앱 3개가 기기에 남아 있었고 이번에 제거했다.
- **새 코드: `reproloop/ios_device_qualification.py`** — `qualify_ios_device()`와 `PhysicalIOSProbeSubject`.
  `mobile-device` 필수 probe 5개(device-boundary / network-boundary / backend-scope / process-termination /
  state-cleanup)를 실기기에서 측정해 `QualificationAuthority`에서만 capability를 발급한다. 저장 JSON·helper 응답으로는
  발급 불가, 새 측정은 이전 qualification 폐기. 문서는 [IOS-PROTECTED-SERVICE.md](docs/IOS-PROTECTED-SERVICE.md) 새 절.
- **실측 발급** ([device-qualification-r1.json](artifacts/product-delivery/d4-ios-device-qualification-r1/device-qualification-r1.json)):
  5 probe 모두 통과, 8.5초, `require_qualification` 수락, capability 미저장. environmentDigest는 대역 값이며
  등록 route digest가 아니다.
- **서명 앱 Keychain 초기화 실측 완료**: 로컬 프로파일로 서명한 fixture `Inventory` 앱(단일
  keychain-access-groups)에서 bootstrap이 선택 항목만 삭제·비선택 항목 유지(-25300/0)를 확인했다.
  비선택 항목은 **앱 삭제·재설치 후에도 유지**돼, 두 항목을 선택한 정책 변형 실행으로 제거했다 —
  재설치는 Keychain 초기화가 아니다. 기기는 최종 상태에서 합성 항목·앱 모두 부재로 확인했다.
  [검증](artifacts/product-delivery/d4-ios-device-qualification-r1/keychain-device/keychain-device-validation.json)·
  [정리](artifacts/product-delivery/d4-ios-device-qualification-r1/keychain-device/keychain-cleanup.json),
  문서는 [IOS-SANITATION.md](docs/IOS-SANITATION.md) 갱신.
- **보호 서비스 공개 설정 초안 + check-config 통과**: QA-iPhone 샘플 대상 `protected-config-draft/`를 생성했다
  ([생성기](artifacts/product-delivery/d4-ios-device-qualification-r1/generate-protected-config.py)).
  실제 파일로 묶은 입력: 3개 서명 baseline IPA(원본/helper-host/helper-runner), devicectl·xcodebuild·
  native ios-device-guardian 바이너리, `ios-signing build-tools` 결과, embedded 프로파일과 그 안의 개발 인증서,
  guest agent 패키지(agentDigest `79967a60`). 결과 `configuration-validated`, digest `c8fc5240`,
  `executionAuthority: "none"` — VM·서명·기기 실행은 시작되지 않는다.
  운영자 잔여 입력: sealed guest bundle과 그 environmentDigest, Apple WWDR/root anchor, PKCS#12 private stream.
- 테스트: `tests/test_ios_device_qualification.py` 11개 통과, `test_execution_*` 83개 통과.
  전체 discover 2175개 중 4개가 부하/모듈 경로 문제로 실패했으나 개별 실행 시 모두 통과(이번 변경과 무관, [로그](artifacts/product-delivery/d4-ios-device-qualification-r1/full-suite-r1.log)).
- 측정하지 않은 것: cleanup 단계 schema 2 영수증·동시 writer 부재(실기기), 대상 앱 자체 네트워크
  트래픽의 backend 격리(helper 제어 포트 범위만 측정됨), 보호 서비스 활성화, D5.

## r53 결과 (2026-09-16, qualification runner probe 보강)

- **`network-boundary` 확장**: helper `live-ios/Tests/LiveControlTests.swift`에
  `nonLoopbackInterfaceAddresses()`(getifaddrs, IFF_UP·비루프백 IPv4/IPv6)를 추가해 `/status`에
  `networkInterfaces`로 보고한다. runner는 tunnel 주소를 제외한 모든 보고 주소로 helper 포트 TCP
  연결을 시도하고 하나라도 성공하면 발급하지 않는다. 필드가 없거나 비목록이면 실패다.
- **`backend-scope` 확장**: 정상 `/activate` 전에 외부 provider incarnation의 grant로 `/activate`를
  시도해 거절을 실측한다(`foreignActivationRejected`). 기존 외부 incarnation 명령 거절·retire 후
  일반 명령 거절과 함께 helper가 세션 incarnation을 진짜로 강제하는지 확인한다.
- **QA-iPhone 재측정 발급**([device-qualification-r1.json](artifacts/product-delivery/d4-ios-device-qualification-r1/device-qualification-r1.json),
  sha256 `d5b6ed2f`): helper가 **32개** 비터널 주소를 보고, 호스트 연결 성공 **0**
  (`nonTunnelInterfaceCount: 32`, `nonTunnelReachableCount: 0`), 5 probe 통과, 23.8초,
  `require_qualification` 수락. r52의 미측정 항목 두 개가 실측으로 전환됐다.
- 테스트 `tests/test_ios_device_qualification.py` 14개 통과(신규: 비터널 도달 시 거절,
  필드 누락 시 거절, 외부 `/activate` 수락 시 거절). Swift helper는 재빌드·재서명해
  `helper-build/runner/Build/Products/`에 배치했다.
- 수정 중 발견한 버그: probe 판정에서 `nonTunnelReachableCount: 0`이 falsy라 `all()` 판정이
  반대로 됐던 문제를 명시적 조건으로 수정했다. 테스트로 검증했다.

## r54 결과 (2026-09-16, 선택적 비격리 빌드 `host-build`)

- 사용자 결정으로 **격리 여부를 운영자가 선택**할 수 있는 경로를 추가했다. 기본은 그대로
  `build-guest`(봉인 VM)이며, `host-build`는 공개 설정에서 `bundlePath` 대신 `hostPath`와
  `executionClass: "host-build"`를 명시할 때만 활성화된다. 둘을 섞는 설정은 거절한다.
- 새 구성 요소: `HostBuildBundle`/`provision_host`(도구를 절대 경로·SHA-256·크기로 고정,
  `host-qualification-probe` recipe 필수, private manifest), `HostBuildBackend`(호스트
  자식 프로세스, 전용 process group, `start_new_session`, 취소/데드라인 시 그룹 kill·수거,
  run 디렉터리 완전 비움 후 저널 종결), `qualify_host_build`(toolchain-boundary·
  process-termination·cleanup 3 probe, 새 측정 전 이전 qualification 폐기).
- **격리로 표시되지 않는다**: 결과·증명에는 `isolation: "host"` / `buildIsolation: "host"`,
  환경은 `network: "unrestricted"`, `transport: "host-process"`만 허용한다. host 번들은
  build-guest route로, guest 번들은 host-build route로 쓸 수 없다(양방향 테스트).
- 계약 변경: `EXECUTION_CLASSES`·`HOST_CONTROLS`, `contracts/project.py`와
  `contracts/execution.py`의 class 열거, `repair_configuration`의 `bundlePath|hostPath` 분기,
  `protected_tool_inputs`의 `build_bundle`과 `buildIsolation` 공개 표시, `protected_service`의
  조합이 번들 타입을 따라간다. `repair-backend-doctor`는 host-build descriptor도 진단한다.
- 테스트 `tests/test_execution_host_build.py` **16개 통과**(번들 생성/변조 거절/probe recipe
  필수/qualification 발급·재측정 폐기/실행 결과 host 라벨/guest↔host 상호 거절/설정 opt-in).
  회귀: `test_execution_*` 96 + `test_repair_*` 167 + protected 입력·설정·조합 40개 통과.
  기존 테스트의 `guest_bundle` 참조를 `build_bundle`로 갱신했다.
- 문서: `docs/PROTECTED-SERVICE-CONFIGURATION.md`에 `host-build` 절과 비격리 주의를 추가했다.

## r55 결과 (2026-09-16, Codex 리뷰 → host-build 보강)

- `codex-review`로 독립 보안 리뷰를 돌렸다 — 결과 `Request changes`, 16개 findings 중
  HIGH 7개를 실제 코드와 대조해 대부분 확정했다.
- 수정한 항목:
  - **복구 권한**: `HostBuildBackend.reconcile`이 VM용 `store.reconcile`을 재사용하던 것을
    제거했다 — 같은 UID의 후보가 `termination.json`을 위조해 quarantine을 풀 수 있었다.
    이제 항상 거절하고 운영자 저널 재설정을 요구한다.
  - **정리 symlink**: `_discard_run_tree`를 fd-기준(`O_NOFOLLOW`+`fstat`+`dir_fd` 상대
    unlink/rmdir)으로 재작성 — run root가 symlink로 교체돼도 대상 경로를 지우지 않는다.
  - **launch 경합**: `Popen` 직전에 취소·데드라인을 재확인 — 이미 취소된 run은 프로세스를
    시작하지 않는다.
  - **kill 무한루프**: 그룹 kill 후 10초 확인 창을 두고, 미확인 시 `stopped=False`로
    저널이 격리 처리한다.
  - **종료 증거 유실**: `run_host_recipe`가 예외 대신 구조화된 결과(lifecycle/stopped)를
    반환 — 스테이징 실패가 backend를 영구 quarantine시키지 않는다.
  - **termination probe**: 취소가 실제로 SIGKILL을 보냈고 자식이 시그널로 종료했는지
    요구(`exitCode == -SIGKILL`) — 자발적 종료 recipe가 종료 측정을 통과하던 구멍을 닫았다.
    worker 예외·미완료도 실패로 기록한다.
  - **qualification 폐기 순서**: 재측정이 받아들여지면 `bundle.verify()` 이전에 이전
    qualification을 폐기 — toolchain 변조로 측정이 실패해도 낡은 자격이 남지 않는다.
  - **결과 타입**: `GuestExecutionResult.isolation`을 필수 필드로 만들고 supervisor가
    `result.isolation == backend.isolation`을 검증 — guest 결과가 host 라벨을, 또는 그
    반대를 달고 나올 수 없다.
  - **실행 scope 안정화**: `machine_digest`를 도구 목록이 아닌 env-id 기준으로 —
    toolchain 교체가 lease scope를 바꿔 기존 마커를 우회하던 경로를 닫았다.
  - **execute 재검증**: launch 직전 `bundle.verify()`로 TOCTOU 창을 좁혔다(완전 해소는
    보호된 toolchain 디렉터리 필요 — 문서화).
- **수정하지 않고 문서화한 한계**(`PROTECTED-SERVICE-CONFIGURATION.md` "알려진 한계"):
  `setsid()` 자손 추적 불가, TOCTOU 잔여, cpu/memory/disk는 권고·예약 값(비강제),
  후보 stdout/stderr 미수집(구조화 reason 코드만).
- 테스트: `test_execution_host_build` **21개**(신규 적대적 5개: reconcile 거절, root
  symlink 교체, 중첩 symlink, 후보 생성 파일 정리, launch 전 취소). 회귀:
  `test_execution_*` 104 + `test_repair_*` 167 + protected/조합/doctor 51개 전부 통과.

## r56 결과 (2026-09-16, Claude 재리뷰 → 2차 보강)

- `claude` CLI로 독립 재리뷰 (packet-ask의 claude 자격증명 부재로 스냅샷 worktree에서
  실행 — 실제 저장소 미노출). Verdict **request changes**, HIGH 2·MEDIUM 7.
- 확정·수정한 항목:
  - **HIGH**: `_execute_owned` 초기 `stopped=False`가 launch 전 실패를 영구 격리시키던
    것 → `stopped=True` 초기화(프로세스 미존재가 정직한 초기 상태).
  - **HIGH**: 그룹 기반 `stopped`가 setsid 탈출 자손·같은-UID 저널 조작을 못 잡음 →
    신호 의미를 문서로 강등(운영 신호, 무결성 증명 아님). 별도 UID 실행은 미지원.
  - **MEDIUM**: `OwnedRun.finish`의 경로 기반 정리가 run dir symlink를 따라감 →
    저널 정리도 fd-기준(`open_directory`+`O_NOFOLLOW`+`dir_fd`)으로 재작성하고 admit
    시점의 (st_dev, st_ino)를 `directory_identity`로 기록·대조. VM 경로도 같이 보호됨.
  - **MEDIUM**: `machine_lease`가 host scope를 'vm' kind로 바인딩 → `kind='host'` 추가,
    저널 scope·lease 이름·`require_scope_available`에 반영 — `store.reconcile`이 host
    저널을 구조적으로 거절.
  - **MEDIUM**: `_remove_tree_fd` 재귀 → 깊이 상한 64 + RecursionError 포착.
  - **MEDIUM**: termination probe가 고정 0.3s sleep 후 취소 → `started` Event로 launch
    관측 후 취소, join 40s(kill 창 커버).
  - **MEDIUM**: 스크립트 도구의 shebang 인터프리터 미검증 → `verify()`가 인터프리터도
    고정 도구 목록에 요구(`/bin/sh` 등 선언 필수), `provision_host`도 verify 실행.
  - **MEDIUM**: `TrustedBuildProof.isolation`의 `'guest-vm'` 기본값 제거(필수 필드).
- 테스트 **23개**(신규: 인터프리터 미선언 거절, runtime 경로 사전-취소). 회귀:
  `test_execution_*` 106 + `test_repair_*` 167 + protected/조합/doctor 51 전부 통과.
- 잔여 LOW: PGID 재사용 false-positive(무해한 격리), `cleaned` 진단 미소비, getuid/geteuid
  표기 불일치 — 수용 판단.

## r57 결과 (2026-09-16, host-build 실물 e2e)

- `ProtectedBuildSupervisor.ready()`가 machine_lease를 kind 없이 호출해 host scope에서
  깨지던 것 수정 — backend에 `scope_kind`('vm'/'host')를 노출.
- `artifacts/product-delivery/d5-host-build-e2e-r1/`: 실제 도구(`xcodebuild`·`zip`·`sh`·
  `sleep`·wrapper 스크립트)를 digest로 pin한 host 번들을 `provision_host`로 봉인 →
  `qualify_host_build` 3 probe 통과 → `ProtectedBuildSupervisor.build`로 ios/ 샘플
  프로젝트를 **실제 xcodebuild로 빌드**(6.6초) → 무서명 `candidate.ipa` 산출·검증
  (`io.reproloop.sample.ios`, `_CodeSignature` 없음 = unsigned) → 저널 `succeeded`,
  run 디렉터리 완전 제거, `buildIsolation: "host"` 표시.
- 의미: `.p12` 서명 입력만 들어오면 보호 서비스의 빌드 단계가 VM 없이 실제로 돌 수
  있음을 코드가 아닌 실행으로 증명했다. `run/result.json` 참고.
- 실행: `run-host-build-e2e.py`는 매 실행 고유 env id로 lease scope를 만들어 재실행 가능.

## r58 결과 (2026-09-16, runner 잔여 미측정 → 저널 레이어 실측)

- `artifacts/product-delivery/d4-ios-device-qualification-r1/native-cleanup-measurement.py`:
  draft의 실제 mobile-definition·실제 QA-iPhone UDID로 `HostAuthority.claim_device`를 수행한 뒤
  진짜 `IOSMobileOperationStore` 생명주기를 구동 — fixture/lab double이 아닌 실제 모듈.
  보고서 `native-cleanup-measurement.json`은 **44 probe 전부 통과**.
- 실측된 것: schema-2 `native.json`(4개 역할 preparedApps digest·generation·incarnation),
  활성 operation 중 동시 claim_device/native_owner/prepare/callback 전부 거절,
  producer.lock·device lease 커널 flock 보유(별개 프로세스·borrowed 디스크립터로 교차 확인),
  `native-finalization/` 영수증(intent `ios-native-finalization-v1` + state `discarded` +
  evidenceDigest가 bindingDigest에 결합), staged 파일 전량 제거·저널 보존,
  admit 종료 후 잠금 해제·owner fencing·`operations.close()`.
- 측정 과정에서 draft 결함 3건 발견(활성화 시 차단될 것): hyphenated runtime-profile id,
  devicectl이 zsh 런처로 pin됨(실제 Mach-O는 CoreDevice 프레임워크 내부), baseline IPA의
  중첩 코드(.dylib/.xctest/.dSYM)가 capability 파서 거절 → 측정에는 repack 사본을 쓰고
  원본/repack digest를 모두 보고서에 기록.
- 여전히 미측정: 대상 앱 자체 네트워크 egress 격리(정책 자체 부재 — helper 제어 포트만
  측정됨), cleanup의 기기 dispatch 구간(서비스 활성화 필요).

## r59 결과 (2026-09-16, draft 설정 결함 3건 원천 수정)

- `generate-protected-config.py` 재생성으로 세 결함을 draft에서 제거:
  1. runtime-profile id를 underscore 문법으로(`repro_sample_ios*` — `id`/`projectId`/
     `applicationId`/`buildId`는 `core.identifier` 검증 대상이라 hyphen 불가),
  2. devicectl pin을 zsh 런처 → 실제 CoreDevice Mach-O로
     (`/Library/Developer/PrivateFrameworks/CoreDevice.framework/Versions/A/Resources/bin/devicectl`),
  3. baseline IPA 정화 — loose `*.dylib`와 버전 메타 없는 `PlugIns/*.xctest`(+페어 `.dSYM`)를
     `make_ipa`가 제외하고 제거 목록을 `draft-summary.json`의 `ipaSanitized`에 기록.
     helper-runner의 유효한 `ReproLiveTests.xctest`+dSYM은 **유지**(제거하면 테스트 실행 불가).
- 검증: check-config 통과(새 digest `56d7d259...`), 3개 baseline 모두 `_extract_ipa` 통과,
  `validate_ios_profile`이 draft를 그대로 수락, `IOSDeviceTools`가 pin 수락.
- `native-cleanup-measurement.py`는 sanitize/override/repack 제거 후 draft 그대로 재실행 —
  **44 probe 전부 통과**, `baselineDigests`가 정의와 일치.

## r60 결과 (2026-09-16, 보호 서비스 실활성화 성공 — QA-iPhone)

- `d6-service-activation-r1/activate-service.py`가 **`status: activated`**를 기록했다
  (`activation.json`). 전체 실경로가 물리 QA-iPhone 위에서 통과:
  device wired → access 할당 → Lab 등록(projectDigest `e34f46a1…` 일치) →
  runtime bundle 조합·검증 → 실기기 qualification **5/5 probe 통과**(23.7초:
  device/network/backend-scope/process-termination/state-cleanup) →
  `compose_service_from_material_stream`으로 private materials 바인딩 →
  build(host-build)·signing·mobile 3 supervisor 조립 + `compose_issue_repairs` attach 완료.
- 설정은 host-build 경로다(사용자 선택). draft digest `670b1781…`, project digest `e34f46a1…`.
- 활성화까지 발견·수정한 설정/코드 결함:
  1. `ios_mobile_inputs.py`·`protected_mobile_inputs.py`가 `build.bundlePath`를 무조건
     참조해 host-build(`hostPath`) 설정이 모바일 입력 로드에서 깨지던 것 — hostPath 분기 추가.
  2. signing-definition.json의 blob 참조에 `bytes` 필드 누락(`_blob_reference` 요구) — 생성기 수정.
  3. provisioning 파일명 `.mobileprovision`이 `safe_transfer_path`/`open_regular`에 거절 —
     `provisioning/dev-wildcard.cms`로 저장하도록 변경.
  4. Lab 실기기 디스크립터 capabilities에 `applicationIdentity`/`applicationProfile`/
     `applicationProfileDigest` 필요 — 활성화 스크립트가 runtime profile에서 도출하도록 수정.
  5. collection policy `captureMode: 'test-data'` 필요(native 디바이스 정책) — 수정.
  6. observer `socketPath` 130B > 104B 한계 — `/private/tmp/reproloop-observer-qa-iphone.sock`으로 단축.
  7. `project.json`의 `recipes`에 build recipe(`host-sample-build`, productFile
     `src/checks/build.json`) 누락 — `ProjectRepair`가 요구. 생성기가 build.json을
     봉인 소스에 쓰고 recipes에 선언하도록 수정.
  8. `~/secure/dev-signing.p12` 권한 `0644` → `0600`(materials loader 요구).
  9. **stale lease cutover 마커**: 재생성으로 mobile journal `environmentDigest`가 바뀌면
     `protected-repair-mobile-device-<scope>` 마커의 `authorityRoot`가 불일치해
     `repair_scope_lease`가 `RunDenied` — 운영자 cutover로 마커 파일 삭제 필요
     (`$TMPDIR/reproloop-leases-<uid>/<key>.authority.json`). signing scope도 동일 주의.
  10. `activate-service.py`가 `work/`를 정리하지 않아 재실행 시 `bootstrap_denied` —
      시작 시 `shutil.rmtree(WORK)` 추가.
- materials 문서(`~/secure/protected-materials.json`)는 `configurationDigest`만 최신값으로
  갱신해 사용한다(비밀값은 저장소 밖 유지). validation secret은 `~/secure/validation-secret.b64`.
- trust anchors: signing definition의 `trust`는 **프로비저닝 프로파일 CMS 검증용**으로
  profile-signing leaf + iPhone Certification Authority(프로파일에서 추출) + Apple Root CA를 쓴다.
  WWDR G3(`~/secure/AppleWWDRCAG3.cer`)는 dev cert 체인용이며 이 필드에는 쓰이지 않는다.
  p12의 실제 발급본(serial `4903…`, 7/8)을 identity cert로 사용 — 프로파일이 두 발급본 모두 허용.

## Current Status (r51 기준, 변경 없음)

- 저장소 위치: `/Users/repro/Desktop/repro-loop`. Git 저장소가 아니므로 branch/commit/PR은 없다.
  로컬 AGENTS.md는 없고 대화에 제공된 전역 지침을 적용했다.
- **r51 소프트웨어·로컬 검증 완료:** iOS 초기화 정책/SDK, trusted adapter와 3회 G4 재생,
  원본 복원, native 재시작 복구, fixture 정리, 서비스 factory·자료 입력·인증 복구 CLI.
- [최종 수용 기록](artifacts/product-delivery/d4-ios-service-r1/acceptance.json):
  **594 검사 통과, skip 0, Python 3.11.15, 고정 입력 596개 불변**.
- [wheel r18](artifacts/product-delivery/d4-foundation-package-r18/dist/repro_loop-0.1.0-py3-none-any.whl):
  **175 모듈·112 리소스**, SHA-256
  `945e004272590dce550d1fa70b3bd5e780341912ae43165e75b106e2c43aa9b9`.
  [새 설치 수용](artifacts/product-delivery/d4-foundation-package-r18/acceptance.json)도 통과했다.
- 이번 인수인계 갱신에서 고정 입력 596개와 wheel SHA-256을 다시 대조했다. 변경·누락은 없다.
  기존 테스트·빌드 결과를 재사용했으며 이번에는 테스트나 빌드를 재실행하지 않았다.
  [최종 검증 연결 기록](artifacts/product-delivery/d4-ios-service-r1/final-verification.json)을 함께 참고한다.
- r50/r17과 모든 실패 이력은 보존했다. 현재 코드에 대해 과거의 “iOS adapter/native 복구 없음”
  설명을 재사용하지 않는다. `artifacts/product-delivery/d4-ios-service-r1/before/`는 r50 입력 571개의 사본이다.

## Completed / Key Files

| 경로 | 현재 역할 |
| --- | --- |
| [IOS-PROTECTED-SERVICE.md](docs/IOS-PROTECTED-SERVICE.md) | 서비스 조합·복구·인증 CLI와 실환경 경계 |
| [IOS-SANITATION.md](docs/IOS-SANITATION.md) | 명시적 파일/defaults/Keychain 초기화 계약 |
| `reproloop/repair_ios.py`, `reproloop/repair_mobile.py`, `reproloop/repair_composition.py` | 같은 owner로 설치·독립 검증·후보 3회 재생·원본 복원과 정리 |
| `reproloop/ios_sanitation.py`, `reproloop/ios_instrumentation_templates/RLSanitationRuntime.swift` | 고정 정책, 시작·종료 초기화 영수증, 실패 시 정상 시작 차단 |
| `reproloop/ios_mobile_xctest.py`, `reproloop/ios_mobile_helper.py`, `reproloop/ios_mobile_runtime_identity.py` | 실제 발급된 launch·native grant·앱 marker·helper/호스트 수거 |
| `reproloop/ios_native_recovery.py`, `reproloop/ios_recovery_execution.py`, `reproloop/ios_recovery_helper.py` | 원래 잠금·새 recovery lease, 이전 helper 부재, bounded 복구 사본·고정 원본 실행 |
| `reproloop/ios_fixture_recovery.py`, `reproloop/ios_recovery_finalization.py`, `reproloop/ios_mobile_finalization.py` | 정확한 fixture 복구, reconciliation, 파일 부재 확인 후 예약·기기 해제 |
| `reproloop/protected_service.py`, `reproloop/protected_service_materials.py`, `reproloop/live/protected_recovery.py` | iOS factory·private 자료 stream·인증 복구 서비스 |

`IOSMobileInputsConfig`의 보호 실행에는 `xctest`와 `sanitation`이 필수다. 공식 준비기가
정책과 digest를 Swift/Info.plist에 넣고 runtime schema 2를 선언한다. 정책 없는 기존 경로는
schema 1이다. 후보의 `artifact.kind: ios-ipa`는 서명 증명의 IPA SHA/크기를 사용하며,
기존 `ios-app`은 tree manifest digest/크기를 유지한다.

정상 종료는 원본 재설치만으로 성공하지 않는다. 새 원본 launch/cleanup 영수증, XCTest의
대상 not-running, helper 종료, native SDK 그룹·출력 수거, fixture 정리, staged 파일 부재가
확인돼야 예약을 반환한다. 초기 검증 실패로 replay가 없을 때도 원래 cleanup capability를 사용한다.

복구 사본은 원래 작업 디렉터리 안에서만 생성한다. 부분 IPA는 등록된 바이트 prefix와,
부분 App은 원본 IPA에서 도출한 남은 파일 집합과 대조해 재개한다. 알 수 없는 파일·링크·
교체된 inode는 보존한다. 최대 3회이고 이전 permit JSON을 재발급 권한으로 사용하지 않는다.
빈/intent-only/단일 검증된 orphan finalization 기록도 실제 새 recovery 권한 아래서만 채택한다.

## Verification

- [고정 통합 gate r2](artifacts/product-delivery/d4-ios-service-r1/service-gate-r2.json):
  65개 관련 모듈·594 검사 통과. 전체 저장소 suite를 모두 재실행한 것으로 표현하지 않는다.
- 첫 [gate r1](artifacts/product-delivery/d4-ios-service-r1/service-gate-r1.json)은 592 통과/1 실패였다.
  실제 guardian/SDK 종료 뒤 collector가 늦게 끝나는 경합을 결정적으로 재현하고,
  join 이후 같은 native 완료 권한을 다시 확인하도록 수정했다.
  [red/green 및 50개 관련 검사](artifacts/product-delivery/d4-ios-service-r1/collector-close-verification.json).
  이후 r2는 수정된 전체 입력에서 한 번에 통과했다.
- [복구 사본 검사](artifacts/product-delivery/d4-ios-service-r1/recovery-material-verification.json):
  실행 20개·결합 71개 검사, fork/os._exit 중단, 부분 추출/삭제와 변조 거절.
  이전 테스트가 남긴 합성 임시 사본 3개는 정확한 내용·inode를 대조해 정리했다.
- [자체 Simulator](artifacts/product-delivery/d4-ios-service-r1/retirement-simulator-r1/acceptance.json):
  native grant 활성화 → `/retire` → 후속 일반 입력 거절 → XCTest exit 0, skip 없음.
  생성한 Simulator 삭제 완료. 앱 경로/defaults 초기화는
  [별도 Simulator 근거](artifacts/product-delivery/d4-ios-service-r1/sanitation-runtime-verification.json)에 있다.
- [새 설치 실행](artifacts/product-delivery/d4-foundation-package-r18/installed-ios-runtime-acceptance-r1.json):
  checkout/test import 차단, 모든 module/resource 해시 대조, 설치된 guardian 컴파일,
  고정 설치·XCTest·schema 2 초기화 영수증 회수·호스트 수거·준비 복구 우회 거절.
  설치본의 전체 protected supervisor를 실행한 검사는 아니며, 그 전체 흐름은 source gate에서 검증했다.
- guardian clang 정적 분석 진단 0개. 지정 범위 독립 검토의 P1/P2 차단 문제는 모두 해결했다.
  SDK/HTTP/VM·일부 서명/관찰은 명시적 자체 대역이며 실제 환경 qualification으로 취급하지 않는다.

## Permissions / Remaining Environment Work

- 이번 요청은 실기기 전 단계에서 끝난다. 실제 기기나 새 외부 네트워크는 사용하지 않았다.
  캐시된 SDK/JDK/clang/OpenSSL, 자체 앱·키/profile·fixture·새 Simulator 사용은 승인된 범위다.
- 사용자 `.env`, 인증 파일, 기본 `.android`, 개인 signing/keychain 자료는 읽거나 변경할 승인이 없다.
  새 네트워크 대상·회사 코드/로그 외부 전송·실기기 조작은 대상/범위와 기존 승인을 확인한다.
  과거 Apple Security GitHub/AOSP GET 및 고정 Android Maven 다운로드 승인 범위만 재사용한다.
- 초기화할 회사 데이터 범위는 운영 입력으로 명시해야 한다. SDK는 concurrent/background/global-init/
  extension/shared/iCloud/WebKit/열린 DB writer를 자동 탐지하지 않는다. 실제 앱에서 계약을 확인해야 한다.
- [Keychain 검증 제한](artifacts/product-delivery/d4-ios-service-r1/keychain-simulator-validation.json):
  unsigned Simulator는 entitlement 오류, ad-hoc entitlement 앱은 실행이 거절됐다.
  실제 서명된 앱의 선택/비선택 Keychain 항목 보존·삭제는 미검증이다.
- 물리 iPhone의 CoreDevice process 목록·tunnel·XCTest 종료와 SDK daemon 경계,
  실제 VM·파일시스템·기기 네트워크/backend 격리의 측정 qualification이 필요하다.
- 정상 보호 실행은 살아 있는 qualification을 받는 service factory다.
  JSON/CLI flag로 qualification을 만들지 않는다. 실제 환경 측정과 정상 서비스 활성화,
  회사 앱·승인된 AI·서로 다른 두 Mac의 수용은 다음 단계다. D4 전체/D5 완료로 표시하지 않는다.

## Next Steps (오픈소스 배포 우선순위)

1. **빌드 경로 선택(r54 이후)** — `build-guest`(봉인 VM, 격리 증명)와 `host-build`(명시적 opt-in,
   비격리·고정 toolchain만) 둘 다 지원한다. VM 없이 끝까지 돌리려면 host-build를 쓴다: 운영자가
   고정 도구 manifest를 `provision_host`로 봉인하고 공개 설정의 `build`를 `hostPath` +
   `executionClass: "host-build"`로 둔다. 엄격한 격리 증명이 목표면 계속 build-guest가 필요하며
   `NativeVM`은 Apple Virtualization guest bundle(고정 이미지·agent·catalog)을 요구한다. 호스트측
   재료는 준비됐다: `build-macos-execution.py`로 컴파일한 vm-helper/guest-connect/guest-run
   (`artifacts/product-delivery/d4-ios-device-qualification-r1/macos-execution-tools/`)과 guest agent
   패키지(`protected-config-draft/guest-agent-package/`). build-guest를 쓰려면 운영자가 전용 macOS
   guest를 설치·구성해 `provision-repair-guest.py`로 봉인해야 한다 — 실제 설치 이미지(IPSW)가
   필요해 네트워크/승인이 있어야 한다.
2. **서비스 활성화(r60 완료)** — QA-iPhone 샘플 서비스가 실기기에서 `activated`까지 통과했다.
   재생성 시에는: materials `configurationDigest` 갱신 + stale lease 마커 cutover(r60 항목 9 참고).
3. **후보 빌드/재현 실주행** — 활성화된 executor로 이제 실제 repair 흐름을 돌릴 수 있다:
   원본 defect 3회 재현 → local-patch(`patch/ios-sample-fix.json`) 적용 →
   host-build 후보 빌드 → signed candidate IPA → QA-iPhone 재생 3회 → 검증.
   이 구간(executor.submit → protected repair 실행)의 실주행 기록이 아직 없다.
4. **runner 추가 보강(선택)** — r53 비터널 도달·외부 `/activate` 거절과 r58의 cleanup 단계 schema-2
   영수증·동시 writer 부재(실기기 scope 저널 레이어, 44 probe 통과)는 실측됐다. 남은 항목:
   대상 앱 자체 네트워크 트래픽 격리(별도 egress 정책 필요), cleanup 기기 dispatch 구간
   (helper cleanup 명령·sanitation 관측·hold 소비 — 활성화됐으니 이제 측정 가능).
   `environmentDigest`도 등록 route digest로 교체할 것(현재 대역 값). r58이 발견한 draft 설정
   결함 3건(hyphenated id·devicectl 런처 pin·IPA 중첩 코드)은 r59에서 원천 수정됐다.
5. **D5 수용(사용자 환경)** — 회사 앱·승인된 AI·두 Mac이 오면 회사 회귀와 단절/재시작/정리 실패 수용 검사.
   `ios-mobile recover`는 준비 전용이므로 native 작업 해제에 사용하지 않는다.

## Resume

승인된 실환경 입력을 받기 전에는 통과한 gate/wheel을 반복 빌드하지 않는다.
새 변경·실패·미해결 위험에 필요한 검사만 실행한다. gate/build harness는 결과 파일을
독점 생성하므로 재실행이 필요하면 새 rN 경로를 사용한다.

안전한 재개 프롬프트:

> `/Users/repro/Desktop/repro-loop`의 HANDOFF.md와 docs/IOS-PROTECTED-SERVICE.md,
> docs/PROTECTED-SERVICE-CONFIGURATION.md를 읽어줘. r52~r53에서 QA-iPhone 실기기 qualification을
> 구현·실측했고, r54에서 선택적 비격리 `host-build` 실행 클래스를 추가했다(테스트 16개 통과).
> VM 없이 진행하려면 host-build 설정(hostPath + host-build route)으로, 격리 증명이면
> build-guest VM bundle 준비(Next Steps 1)로 이어가줘. QA-iPhone은 잠금 해제 상태여야 한다.
