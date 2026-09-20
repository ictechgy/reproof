# Handoff

_Last updated: 2026-09-18 by devin (r63)_

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

## r62 결과 (2026-09-18, 성능 개선 BlobSet 해시 캐싱 + 저널·보안 분석 — 미커밋)

- **요청**: HANDOFF(r61) 기준 "툴 보안 구조 성능 개선 파악" → "개선해줘" → "나머지 개선들도".
- **적용한 개선(미커밋, `main`에 working tree로 존재)**:
  - `reproloop/execution/artifacts.py` — `BlobSet`이 매번 전체 바이트를 재해시하던 것을
    생성 시 1회 계산 후 `_files`(path, digest, size)·`_digest`로 캐싱(`field(init=False,
    repr=False, compare=False)`, slots 호환). `manifest`는 캐시에서 새 dict를 만들어
    반환하므로 호출자가 manifest를 변조해도 내부 해시가 오염되지 않는다. 측정: 8MiB
    입력 digest 20회 조회 0.051초 → 0.0000005초(Python 3.14 단순 측정, 전체 실행 수치 아님).
  - `tests/test_execution_artifacts.py` — 회귀 2건 추가: 해시 재계산 부재
    (`patch.object`로 sha256 미호출 검증), 반환된 manifest 변조·항목 재정렬이
    캐시된 digest를 바꾸지 않는지. 산출물·실행 관련 테스트 **41개 통과**
    (`python3.11 -m unittest tests.test_execution_artifacts tests.test_execution_runtime
    tests.test_execution_host_build -q`).
- **파악만 하고 적용하지 않은 것(다음 세션 후보)**:
  - 저널 512건 누적 제한(`reproloop/execution/journal.py:19,157,203,540`): terminal 기록
    아카이빙 설계까지 조사 완료(옵션 A: 레코드당 archive 파일 + state 병합, 옵션 B/C
    비권장). fail-closed 조건: terminal+reservedBytes==0+흔적 부재만 이동, archive commit
    후 state 삭제 순서, 중복 ID 영구 거부, 읽기 preflight 무쓰기, 아카이브 변조 거절.
    docs/REPAIR-EXECUTION.md:166 "512 identities, 무음 eviction 금지" 준수 필요.
  - 플랫폼 복구 코드가 공통 저널에 직접 import되는 결합
    (`journal.py` 내 `finish_mobile_recovery` 등 5개 — 책임 분리 별도 작업).
  - qualification 시간 제한·취소 경계(`reproloop/ios_device_qualification.py`) 강화 미착수.
  - 보안 우선순위(측정 필요, 코드 변경 아님): host-build는 격리 아님, 대상 앱 자체 네트워크
    egress 격리는 별도 정책 필요(HANDOFF r58/r61의 미측정 항목과 동일).
- **기존 실패(이번 변경과 무관)**: Python 3.14 전체 suite는 120초 제한으로 중단. 그 안의
  기존 실패 4건 확인 — `test_android_app_logs_runtime`(Kotlin 컴파일러 클래스 누락
  `ClassNotFoundException: org.jetbrains.kotlin.cli.jvm.K2JVMCompiler`), Android native
  frame/observation 3건. r51 gate는 Python 3.11.15 기준 594 통과였다. lint·타입 검사
  설정은 저장소에 없음(pyproject.toml에 setuptools뿐, ruff/mypy 정의 없음).
- 커밋·브랜치·push 없음. 다음 세션에서 위 미커밋 diff를 검토 후 커밋하거나 이월 판단.

## r63 결과 (2026-09-18, r62 잔여 개선 완료 — 저널 아카이빙 + 복구 분리 + qualification 경계)

- **요청**: HANDOFF(r62)의 안전한 재개 프롬프트 그대로 — 미커밋 diff 검토·커밋 후
  "적용하지 않은 것" 3건을 순서대로 진행.
- **커밋(전부 `main`)**:
  - `2df13b1` — r62 미커밋분 커밋: BlobSet manifest/digest 해시 캐싱(41 테스트 통과 확인 후).
  - `f5dc0c6` — HANDOFF r62 갱신분 커밋.
  - `8463b8c` — 실행 저널 terminal 레코드 아카이빙.
  - `7ff93c1` — 플랫폼 복구 종결 로직 `journal_recovery.py` 분리.
  - (아래 qualification 경계 변경은 별도 커밋 — 해시는 git log 참고.)
- **저널 512건 terminal 아카이빙(`reproloop/execution/journal.py`, 옵션 A 구현)**:
  - `archive/<operation_id>`에 레코드당 1파일(canonical JSON: `schemaVersion/operationId/
    requestDigest/state/reservedBytes`), temp+`os.replace` 원자 커밋, 0600.
  - `state.json`에 `archive: {count, digest}` 메타데이터 — 아카이브 집합 전체에 대한
    aggregate 바인딩. 기존 저널(archive 디렉터리 없음)은 그대로 읽힘.
  - fail-closed 조건 전부 구현: terminal+`reservedBytes==0`+`runs/<id>` 디렉터리 부재만
    이동 / archive 파일 commit → state 삭제 순서 / archived ID 영구 재사용 거부 /
    `require_available`·`admit`이 거절 전 아카이빙 시도 / 변조·삭제·불일치·외국 레코드 거절.
  - 크래시 윈도우: archive commit 후 state 미갱신이면 orphan이 `runs`에 terminal로 남고,
    다음 로드·아카이빙에서 내용 일치 검증 후 채택.
  - `docs/REPAIR-EXECUTION.md:166`의 "512 identities·무음 eviction 금지" 문구를
    아카이브 동작에 맞게 갱신. 테스트 12개 추가(`tests/test_execution_journal.py`),
    저널 관련 suite 통과.
- **저널-복구 결합 분리(`reproloop/execution/journal_recovery.py` 신규)**:
  - `finish_mobile_recovery`·`finish_ios_native_recovery`·`finish_ios_preparation_recovery`·
    `consume_ios_native_disposal`·`finish_signing_recovery` 5개 본문을 이동.
    `RunStore`는 lazy import delegate 메서드만 유지(공개 API·호출부 ~30곳 무수정).
    journal.py에서 플랫폼 모듈 직접 import 제거 → 순환 참조 위험 축소.
  - 복구 관련 129 테스트 중 126 통과. 실패 3건은 전부 네이티브 툴체인 환경 문제로
    변경 전 stash 상태에서도 동일 재현 확인: Android guardian 컴파일 실패,
    iOS native pipeline·signing — `Xcode-27.0.0-beta.app`의 `MacOSX27.0.sdk` sysroot 부재.
- **qualification 시간 제한·취소 경계(`reproloop/ios_device_qualification.py`)**:
  - `_devicectl` — `subprocess.run(timeout=60)` 고정을 Popen+poll로 교체.
    `deadline_monotonic`·`cancellation` 인수 추가, 호출당 상한은 `min(60s, 잔여 데드라인)`.
    취소/만료 시 `SIGKILL`로 프로세스 그룹(`start_new_session`) 정리 후 fail.
  - `PhysicalHelperSession.wait` — 전체 잔여를 한 번에 `wait(timeout=)` 하던 것을
    0.2초 poll 루프로 교체, 매 반복 취소·데드라인 검사.
  - `_probe_network_boundary` — 비터널 주소 스캔이 데드라인/취소로 잘리면
    `nonTunnelScanComplete: false`로 기록하고 probe 실패(fail-closed). 잘린 스캔의
    `reachable==0`을 "전부 닫힘" 증거로 쓰지 않는다. connect 타임아웃도 잔여 데드라인으로 상한.
  - `_probe_process_termination` — helper 프로세스 소멸 대기 루프에 취소·데드라인 검사와
    `observationComplete` 증거 추가. `_probe_state_cleanup` — uninstall 루프에 경계 검사와
    `uninstallCompleted` 증거 추가. 모든 subject devicectl 호출에 경계 전파.
  - 테스트 4개 추가(측정 중 취소가 probe 경계에서 중단, helper 대기 중 취소,
    잘린 스캔 fail-closed, subject 호출에 데드라인 전달). **18개 전부 통과**
    (`python3 -m unittest tests.test_ios_device_qualification`).
  - 잔여 known gap: `device_status`의 `select_iphone`(query_client 없음)은 내부
    devicectl 호출당 30초 고정 상한으로만 묶여 있고 qualification 데드라인을 직접 받지
    않는다(≤60초 residual, probe 경계에서 전체 데드라인은 여전히 강제됨).
- **다음 세션**: qualification 경계 커밋까지가 r63 범위. 실주행(QA-iPhone 재연결·
  `run-issue-lifecycle.py`)은 사용자 승인 후.

## r61 결과 (2026-09-17, 실주행 runner 준비 + 기기 차단 + README 퇴고 + git init)

- **실주행 runner 작성**: `artifacts/product-delivery/d6-service-activation-r1/run-issue-lifecycle.py`.
  활성화(activate-service.py와 동일 경로)에 이어 record → stop → save_specification →
  approve → replay(3회 캠페인) → `repairs.start(mode: verify)`까지 한 스크립트로 구동한다.
  물리 iOS는 locator·semantic observe를 지원하지 않으므로:
  - 입력은 `parameters.{x,y}` 좌표 탭 + `geometry`(width/height/rotation/version)만 사용한다.
    `workflow.input`에는 `controllerId`/`epoch`/`sequence`/`operationId`가 필요하다.
  - 버튼 위치는 `tap-target-probe.swift`(Vision OCR)가 세션 프레임에서 텍스트로 찾는다 —
    이전 실기기 프레임에서 `Add`를 (0.480, 0.439)로 확인했다.
  - 관측은 `DeviceFrameObservationAdapter`(로컬 클래스)를
    `runtime.service.runner.observations.register('screen', …)`로 등록한다 — 프레임을
    `scripts/capture-text-probe.swift`로 OCR해 카운터 값을 `text` property로 돌려준다.
    project.json이 observation `screen`을 선언한다.
  - `iphone_device()`는 `ios-app` kind만 받으므로(이 프로필은 `ios-ipa` 선언) 디바이스
    디스크립터+provider factory를 직접 만든다. `capabilities`에 `applicationIdentity`/
    `applicationProfile`/`applicationProfileDigest`가 필요하다(r60 항목 4와 동일 요구).
    `original.ipa` 내 `Payload/ReproSample.app/ReproSample`과 설치용 `.app` 바이너리의
    sha256 일치를 스크립트가 직접 고정한다(IPA 컨테이너 digest ≠ .app tree digest이므로
    전체 트리 비교는 유효하지 않다 — 바이너리 수준 대조가 정석).
  - 첫 프레임 대기는 `lab.frame` 재시도 60회×0.5초, 실패 시 `frame_pending`.
- **오프라인 계약 검증 완료**: `get_session`(controllerId/epoch), `frame()` 필드,
  `save_specification`/`get`(campaign 포함)/`jobs.start`·`get`(`{'repair':…}`, TERMINAL 집합)
  반환 구조를 코드와 대조해 확인했다. 실주행은 아직 없다.
- **차단: QA-iPhone 연결 끊김** — `devicectl`이 `unavailable`을 보고한다
  (`wired: False`, `tunnelConnected: False`). 기기가 돌아오면 status 재확인 후
  runner를 실행하면 된다. 사용자가 "잠시 후 계속"을 선택해 이 상태에서 멈췄다.
- **README 퇴고(영어 기본 + 한글)**: `README.md`를 영어로 재작성(오픈소스 표준),
  `README.ko.md`를 한글로 분리했다. 구조를 "소개 → 현재 상태(검증됨/남은 것 구분) →
  제품 방향 → 샘플 대상 → iOS → Android → 수정 루프 → 결과 → 테스트 → 구성 → Live 콘솔"로
  정리하고, stale 주장(활성화 전 시점 기술)을 r60 결과로 갱신했다. 한글판은 claude CLI
  2회 리뷰를 거쳤다(번역투 12건·용어 통일·사실 정밀화 반영). PRODUCT-DELIVERY-PLAN.md는
  r51 시점에 머물러 있어 README에 "계획 문서는 r51 기준" 주석을 달았다 — 계획 문서 자체
  갱신은 아직이다.
- **git 초기화**: 저장소를 git으로 만들었다. `main` 브랜치, 초기 커밋 `7a59898`
  (766 파일, ~10.5MB). `artifacts/`(8.4GB)·Xcode/Gradle 빌드 산출물·`.kotlin/`·
  `.omc/`·`.DS_Store`는 `.gitignore`로 제외. 커밋된 파일에 비밀 패턴 없음을
  git grep으로 확인했다(서명 자료는 저장소 밖 `~/secure/`에 있다). 원격 remote·push는
  아직 없다.

## r64 결과 (2026-09-19, 실기기 보호 라이프사이클 최초 완주 — repair `verified`)

`run-issue-lifecycle.py`가 QA-iPhone에서 **처음으로 끝까지 완주**했다:
qualification(`qualified`, 26.9s) → compose(`protectedRepairsAttached`) → fixture 준비
(port 8766) → record(`issue_1c43efc0515641b7a156f8befdb3f3de`) → 승인
(spec `5850adc1…`) → replay(`reproduced`, 3회 모두 `observed`) → repair
`repair_4066412435034cb1877d85e7a551a28f_mobile` = **`verified`**.
cleanup 관측은 `clean=True`(processes/fixtures/sanitation/scopeReleased 전부 True),
저널은 run `succeeded` + `reservedBytes: 0`, 기기는 프로세스 0개·원본 앱 복원 상태다.

이번 라운드에 누적된 실기기 경로 수정(전부 재생 검증됨):

- **CoreDevice 터널 홀드** — XCTest는 idle ~3–10s면 터널이 죽는다. `devicectl device
  notification observe`는 XCTest의 socket-ID 핸드셰이크를 깨뜨려 부적합.
  `devicectl device motion spatial-orientation`(스트리밍)으로 세션 동안 터널을
  유지하고 `session.close()` 후 해제한다 (`ios_mobile_xctest.py` `_tunnel_holds`).
- **helper 기동 대기** — XCTest 부팅(~수십 초) 동안 `/status`는 connection refused.
  `allow_initial` 폴링이 transport 실패를 `None`으로 변환해 바인드까지 재시도한다
  (`ios_mobile_helper.py`). Android 경로와 같은 의미다.
- **`/status` 프로토콜** — helper가 진단용 `networkInterfaces`를 보낸다. validator가
  타입 검사 후 허용하도록 fail-closed 유지하며 추가했다.
- **runtime-v1 앱 런치 모드** — 샘플의 `uikit-runtime-v1` 프로필은 `REPRO_MODE=record`
  +`REPRO_CASE`만 수용하고 `observe`는 `uikit-observation-v2` 전용이다. runner가
  non-observation 프로필에 `REPRO_LIVE_CASE`를 렌더링하고, helper `launchTarget`이
  그 유무로 record/observe를 선택한다 (`LiveControlTests.swift`). 이전에는 항상
  observe로 떠서 앱이 identity를 쓰지 않았다.
- **프레임 폴링 404 계약** — `NativeFrameBuffer.after(cursor)`의 `?? frames.last`
  폴백이 이미 소비한 프레임을 재반환해 fail-closed 검증을 깼다. 서버는 `id > cursor`
  없으면 404만 반환하고, 클라이언트는 `last_native_frame > 0` 이후의 404를
  "새 프레임 없음"으로 허용한다 (record 경로가 암묵 의존하던 동작).
- **sanitized variant entitlement** — `sample-build-sanitized`만
  `keychain-access-groups`가 빠져 `keychainAccessGroup(required:)`이 `_exit(78)`로
  죽었다(= 세션 deadline 격리의 근본 원인). variant에 entitlement 파일 +
  `CODE_SIGN_ENTITLEMENTS`를 주입하고, `prepare-sanitized-sample.py`의
  `keychainGroup` 체크를 `codesign --entitlements :-` XML 파싱으로 바꿔
  `application-identifier` substring false-positive를 없앴다.
- **네이티브 finalization 분류** — guardian이 `TMPDIR=<work>`를 하드코딩해
  SwiftPM이 `_Users_repro_.swiftpm.lock`(escaped `~/.swiftpm` + `.lock`)을
  work 최상위에 만든다. `_GENERATED_WORK_FILES`에 계산된 이름으로 분류해
  `_command_record` 검증을 통과시켰다 (`ios_mobile_finalization.py`).

재생성/재바인딩: original.ipa 교체로 protected-config 재생성(config digest
`d0c2272b…`) + materials 재바인딩 + stale mobile scope 마커(`bfa2b936`, 구
authorityRoot `3f9a1c45`)만 제거했다. `6e8967bb`(현재 mobile scope)는 유지.

**커밋 상태**: 이번 실기기 라운드의 수정은 `fix/ios-device-lifecycle` 브랜치에
6개 커밋으로 정리됐다(provisioning CMS·keychain 정책·XCTest 세션·finalization·
cleanup 타임아웃·이 문서). `main` 워킹 트리는 깨끗하다. 디버그용
`DIAG`/`traceback.print_exc` 계측은 검증 후 전부 제거했다.

## r65 결과 (2026-09-19, 실기기 negative path 검증 — 거절 경로 3종 증명)

r64가 성공 경로(`verified`)를 증명했다면, r65는 보호 검증이 **틀린 후보를 정확히
거절하는지** 실기기에서 확인했다. `issues.json`의 `repair.agent.patchFile`을
일시적으로 bad patch(`patch/ios-sample-badfix.json`, 유지됨)로 바꿔 주입했다 —
patchFile은 경로 참조라 digest 재바인딩 없이 교체 가능하다.

세 번의 실주행:

1. **editable 경로 외 패치 → `protected_path` 거절** — patch가
   `CounterViewController.swift`(project `editablePaths`는 `CounterLogic.swift`
   하나뿐)를 건드리자 후보 빌드 전에 거절됐다. 모바일 run 레코드조차 생성되지
   않음 — 가장 얕은 방어선.
2. **`return 3` 후보 → 기기 regression validation `failed`** — editable 경로만
   건드리는 후보는 빌드·서명·설치까지 진행되고, `ios-device-regression`
   (`external-observation`, observer 소켓 경유 화면 판독)이 counter=3을 읽어
   `status: failed`로 거절했다. replay 시도는 한 번도 디스패치되지 않았다
   (`attempts: []`). 이 run의 cleanup은 잔여 앱 프로세스로 종료 미확인 →
   `mobile_quarantined` + ~5GB reservedBytes 보류 — **확인 불가 정리를
   "깨끗"으로 보고하지 않고 격리·보류하는 fail-closed도 설계대로 동작**.
   (수동 종결 후 재시도)
3. **같은 `return 3` 후보 재시도 → `failed` + `regression_failed`** — validation
   `failed` 후 cleanup이 전부 확인됨(processes/fixtures/sanitation/scopeReleased),
   저널 `failed` + `reservedBytes: 0`, 기기 프로세스 0개. 설계된 clean rejection.

구조적 발견: 이 샘플 프로젝트는 `editablePaths`가 `CounterLogic.swift` 하나이고
후보 빌드는 `ReproSample` scheme만 컴파일한다(LogicTests 미실행). 따라서
**`candidate_mismatch`(기기 replay verdict 불일치)는 이 프로젝트 구조상 도달
불가** — defect-preserving 후보는 전부 device-regression validation이 먼저
거절한다. replay 수준 불일치를 검증하려면 editable 범위를 넓히거나
UI-only regression을 가진 다른 샘플이 필요하다.

거절 계층 요약(얕은→깊은): `protected_path`(패치 경계) → 빌드 실패 →
`device-regression`(기기 validation) → `candidate_mismatch`(replay verdict) →
`mobile_quarantined`(cleanup 미확인, 자원 보류). r65에서 1·3·5번을 실증했다.

## r66 결과 (2026-09-20, 실기기 중단/복원력 검증 + 재부팅 내구성 발견)

**시나리오**: 라이프사이클 실행 중 candidate cleanup 단계에서 runner를 `kill -9`.
(관찰상 kill은 restore-original dispatch 직후·original sanitation XCTest 직전에
떨어졌다 — 당시 observer 미기동으로 validation이 즉시 실패해 cleanup이
진행 중이었다. replay XCTest 도중의 kill은 별도 변형으로 남는다.)

검증된 동작:

1. **크래시 상태가 durable하게 남는다** — 저널 `repair_e514…_mobile`이
   `admitted` + `reservedBytes: 5,039,029,347`으로 잔존, op dir은 install/
   restore-original dispatch까지 기록된 채 중단. stale lease 마커
   (`protected-repair-mobile-device-6e8967bb…`)도 남았다.
2. **runner 자식은 부모 죽음에 살아남는다** — `fixture-service.py`가 포트를
   잡은 채 고아로 생존. xcodebuild/devicectl 고아는 없었다(해당 시점에
   활성 세션 없음).
3. **다음 라이프사이클은 compose에서 거절된다** — `ProtectedMobileSupervisor.
   ready()` → `store.require_available()`이 비종결 run을 발견해
   `RunDenied('Execution scope is busy or quarantined')` →
   `protected_service_mobile` → 라이프사이클 `failed`. **고아 불확실 작업이
   있으면 새 admit 이전에 compose 자체가 잠긴다** — admit 시점의 자동 격리
   전환보다 앞선 방어선이며, 어떤 경로로도 조용한 재시작이 불가능하다.
4. **운영자 종결 후 완전 복귀** — 기기 상태 검증(restore-original이
   `tool-succeeded`로 끝나 원본 복원 완료·프로세스 0) 후 run을 `failed` +
   `reservedBytes: 0`으로 종결·run/ops 디렉토리 제거·고아 fixture 종료 →
   다음 실행이 qualification→…→repair `verified`까지 완주
   (`repair_75d22ebb731e449da8bd511e03abc15f`).

**재부팅 내구성 발견(실 결함)**: `IOSMobileOperationStore`가 `intent.json`의
configuration digest에 `operationsIdentity`(st_dev+inode+mode+uid)를 넣어
검증한다. macOS 재부팅/재마운트로 `st_dev`가 재할당되면(관찰: 16777234→
16777230, inode 동일) digest가 어긋나 compose가 `protected_service_mobile`로
**영구 거절**된다 — inode만으로 교체 감지는 충분하므로 st_dev 바인딩은
재부팅마다 서비스를 깨는 취약점. 복구는 `mobile-owner/`를 아카이브+재생성
(전부 terminal run일 때 안전). 코드 수정 후보: durable identity에서 st_dev
제외(같은 부팅 내 일관성만 검사)하거나 명시적 re-key 경로 추가.

**부수 발견**: `RepairJournal`/`open_directory`는 경로의 symlink 구성요소를
거절한다 — `$TMPDIR`(`/var/folders` → `/private/var`) 아래 work root는 항상
실패한다. 작업 디렉토리는 실경로에 둘 것.

**r66 변형 2 — replay XCTest 도중 kill(더 깊은 크래시 지점)**: observer 기동 +
정상 패치로 candidate replay 세션이 올라간 뒤(`command-xctest-candidate-001-work`
출현 + 8초) runner를 `kill -9`. 결과:

- 저널 동일하게 `admitted` + ~5GB 보류, 다음 실행도 같은 compose 거절 게이트로
  차단됨.
- **터널 홀드 프로세스(`devicectl device motion spatial-orientation`)가 고아로
  영구 생존** — 세션 종료 시 해제되는 설계라 runner가 죽으면 CoreDevice 터널을
  누가 잡은 채로 남는다. 수동 종료 필요. 후속 후보: 부모 사망 감지 후 자체 종료
  (파이프/watchdog).
- 기기 측 XCTest 세션은 runner 사망과 함께 정리됐다(work dir 상태 `host-stopped`,
  기기 프로세스 0) — **기기 측 세션은 호스트 죽음을 따라가지만 호스트 측
  고아(터널 홀드·fixture 서비스)는 남는다**.
- cleanup이 한 번도 디스패치되지 않았으므로 **candidate 앱이 기기에 설치된 채로
  남았다** — 운영자 종결 시 프로세스뿐 아니라 *설치된 앱의 정체*까지 확인해야
  한다(이번엔 sanitized original .app을 devicectl로 수동 복원).
- 종결 후 재실행 → `repair_684974f275824c93be2e830de5464e87` `verified` 완주.

## r67 결과 (2026-09-20, st_dev durable identity 결함 수정 — `fix/ios-st-dev-durable-identity`)

r66에서 발견한 재부팅 내구성 결함을 코드로 수정했다:

- `IOSMobileOperationStore`의 durable `operationsIdentity`에서 `device`(st_dev)
  필드를 제외했다 — inode/mode/uid만 digest에 바인딩된다. st_dev는 마운트마다
  재할당되는 식별자라 durable identity가 아니다.
- 공유 헬퍼 `_same_identity`의 비교에서도 `device`를 제외했다 — 같은 런타임의
  라이브 검사는 inode 기반 교체 감지를 그대로 유지하며, op 수준 durable 기록
  (`directoryIdentity`/`producerIdentity`/role 기록)도 재부팅 후 복구 경로에서
  유효하게 남는다. Android 스토어는 durable config에 fs identity를 두지 않아
  영향이 없다.
- 구형 `intent.json`(device 포함)은 **device 필드만 제거했을 때 digest가
  일치하는 경우에 한해** `_replace_at`으로 정규화 재기록 후 수용한다 —
  그 외 어떤 필드 변조도 여전히 거절된다(명시적 1회 마이그레이션, 묵시적
  재해석 아님).
- 테스트 4개 추가(`tests/test_ios_mobile_operation.py`): 새 스토어는 device를
  기록하지 않음 + 레거시 device 레코드 정규화 재오픈, device 외 필드 변조
  (inode/environmentDigest) 거절, operations 디렉토리 교체(신규 inode) 거절.
- 실제 `mobile-owner` 스토어로 정규화 경로 검증: 레거시 레코드(device=16777230
  포함)를 열자 device가 제거된 채 원자적으로 재기록됐고 digest가 일치했다 —
  이제 재부팅해도 아카이브+재생성 없이 compose가 열린다.
- 검증: `test_ios_mobile_operation` 19개 + iOS 부분집합 70개 통과. 전체 스위트
  2053개의 실패/에러는 전부 사전 존재 환경 문제(Xcode-27.0.0-beta SDK 경로
  부재·브라우저/워커 환경)로 `main`에서 동일하게 재현됨을 확인했다.

같은 브랜치에서 r66 변형 2의 두 번째 발견인 **터널 홀드 고아**도 수정했다:

- `_spawn_tunnel_hold`가 `devicectl device motion spatial-orientation`을
  watchdog 프로세스(경량 `-c` 감시자)로 감싼다. runner만 쥐는 liveness
  파이프를 두고, watchdog은 파이프 EOF를 보면 모니터를 종료한 뒤 스스로
  끝난다 — 명시적 해제든 runner SIGKILL이든 커널은 같은 EOF를 만든다.
- `_stop_tunnel_hold`는 파이프 닫기로 통일했고, watchdog가 응답하지 않으면
  세션 리더 프로세스 그룹 전체를 마지막 수단으로 kill한다.
- 테스트 2개 추가(`tests/test_ios_mobile_xctest.py`, 총 23개 통과): write
  end를 닫으면(runner 사망과 동일 이벤트) watchdog이 모니터를 회수하고
  프로세스 그룹이 비는 것, 명시적 해제도 같은 경로로 정리되는 것.
- 잔여 한계: watchdog 자체가 SIGKILL되면 모니터는 여전히 고아가 될 수 있다
  (기존과 동일한 최악 케이스, 더 나빠지지 않음). fixture-service 고아는
  딜리버리 스크립트(run-issue-lifecycle.py)의 자식이라 같은 파이프 패턴을
  하니스에 적용할지는 별도 결정.

세 번째 r66 후속인 **sanctioned 운영자 종결(close-run) 경로**도 같은
브랜치에서 추가했다(`reproloop/ios_mobile_close.py` + `ios-mobile
close-run`):

- `close_run`이 원본 `operationId`·`requestDigest`를 검증하고, native-bound
  run은 `--device-clean` 운영자 증명을 요구하며, 저널 종결(`failed`+예약 해제)
  → 소유 op/run 디렉토리 제거까지 수행한다. 수동 `state.json` 편집과
  디렉토리 수동 삭제를 대체한다.
- CLI는 `--config` 공개 참조 경로와 `--owner`/`--runs`/`--udid` 부트스트랩
  경로 둘 다 지원하며 두 입력 집합의 혼합·누락을 거절한다. 부트스트랩은
  owner의 `intent.json`에서 정의·환경 digest를 재구성해 참조 없이 재연다.
- 이미 종결된 run은 `already-terminal`로 멱등 보고하고 외래 디렉토리 내용은
  거절한다. 검증된 owner가 없으면 디렉토리 제거는 일어나지 않는다.
- 실스토어 검증: `protected-config-draft`의 live `mobile-owner`를 부트스트랩으로
  재열어 종결된 실제 run에 `already-terminal` 반환 확인. 발견한 버그 1건 —
  `_open_bootstrap`이 상대 경로를 그대로 넘겨 `_walk_directory`의 절대 경로
  요구에 걸려 generic `ios_mobile_operation_unavailable`을 반환하던 것을
  `resolve()`로 수정.
- 테스트 5개 추가(`tests/test_ios_mobile_cli.py`, 총 13개 통과): 격리 run
  종결, 멱등 재종결, native-bound `--device-clean` 요구, 부트스트랩 재개,
  외래 run 내용 거절, 혼합·누락 입력 거절.

## Current Status (r51 기준 + r66 갱신)

- 저장소 위치: `/Users/repro/Desktop/repro-loop`. **r61부터 git 저장소다**
  (`main`). r63에서 r62 잔여 개선 3건(저널 아카이빙·복구 분리·qualification 경계)을
  완료·커밋했다 — 최신 커밋은 `git log` 참고(r63 절에 나열). remote는 아직 없다.
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
3. **후보 빌드/재현 실주행 — r64에서 완주** — QA-iPhone 실기기에서 qualification→
   record→approve→replay(`reproduced`)→repair **`verified`**까지 전체 보호
   라이프사이클이 완주했다(r64 절의 run/증거 식별자 참고). r65에서 negative
   path 3종(protected_path·regression_failed·mobile_quarantined), r66에서
   중단/복원력 2회(cleanup 도중 kill + replay XCTest 도중 kill → 고아 admitted
   → compose 거절 → 운영자 종결 → verified 복귀)까지 실기기 증명했다. 남은
   변형: 다른 fixture/case 조합, replay 수준 불일치(editable 범위 확장이나
   UI-only regression 샘플 필요). ~~터널 홀드 고아 자체 종료~~는 r67에서
   liveness 파이프 watchdog으로 해소됐다.
   r64 수정분은 `main`에 머지됐다.
4. **runner 추가 보강(선택)** — r53 비터널 도달·외부 `/activate` 거절과 r58의 cleanup 단계 schema-2
   영수증·동시 writer 부재(실기기 scope 저널 레이어, 44 probe 통과)는 실측됐다. 남은 항목:
   대상 앱 자체 네트워크 트래픽 격리(별도 egress 정책 필요), cleanup 기기 dispatch 구간
   (helper cleanup 명령·sanitation 관측·hold 소비 — 활성화됐으니 이제 측정 가능).
   ~~터널 홀드 watchdog~~·~~sanctioned 운영자 종결(close-run)~~·~~st_dev durable
   identity 바인딩~~은 r67에서 해소됐다. 또한 실기기 실행 전에 observer
   (`artifacts/product-delivery/d6-service-activation-r1/observer-service.py`)를
   띄워야 한다 — 미기동 시 `ios-device-regression` validation이 즉시 실패한다.
   `environmentDigest`도 등록 route digest로 교체할 것(현재 대역 값). r58이 발견한 draft 설정
   결함 3건(hyphenated id·devicectl 런처 pin·IPA 중첩 코드)은 r59에서 원천 수정됐다.
5. **D5 수용(사용자 환경)** — 회사 앱·승인된 AI·두 Mac이 오면 회사 회귀와 단절/재시작/정리 실패 수용 검사.
   `ios-mobile recover`는 준비 전용이므로 native 작업 해제에 사용하지 않는다.

## Resume

승인된 실환경 입력을 받기 전에는 통과한 gate/wheel을 반복 빌드하지 않는다.
새 변경·실패·미해결 위험에 필요한 검사만 실행한다. gate/build harness는 결과 파일을
독점 생성하므로 재실행이 필요하면 새 rN 경로를 사용한다.

안전한 재개 프롬프트:

> `/Users/repro/Desktop/repro-loop`의 HANDOFF.md를 읽어줘. 저장소는 git `main`이고
> r64(verified 완주)·r65(negative 3종)·r66(SIGKILL 중단/복원력)까지 실기기 증명됐다.
> r67에서 st_dev durable identity 결함·터널 홀드 고아 watchdog·운영자
> close-run 경로(`ios-mobile close-run`)까지 해소됐다(구형 스토어는 첫 오픈 시
> 자동 정규화). 실기기 실행 전 observer-service.py 기동이 필요하고,
> 다음 실주행은 QA-iPhone USB 연결·잠금 해제 후 run-issue-lifecycle.py로 돌린다.
> 실기기 작업이 필요해지면 먼저 나에게 승인을 요청해줘.
