# 보호 서비스 설정과 Android 복구 운영

2026-09-14 · D4 진행 중

`protected-service check-config`는 보호 실행 설정과 공유 이슈 설정의 연결 관계를
검사한다. 설정에 적힌 도구·VM·서명 정의·기기·관찰자 파일은 열지 않는다.
실제 qualification이나 실행기를 만들지 않으며 성공 결과에도
`executionAuthority: "none"`을 명시한다.

```sh
reproof protected-service check-config \
  --config "$PROTECTED_SERVICE_CONFIG" --issue-config "$ISSUE_CONFIG"
```

두 경로는 운영자가 선택한 공개 JSON이다. 보호 설정은 소유자 파일·단일 링크·
심볼릭 링크 없는 경로·안전한 쓰기 권한·최대 64 KiB·중복 키 거절 규칙을 따른다. 결과에는 설정 digest와
profile ID만 반환하며 입력 경로·본문을 오류에 출력하지 않는다.

## 설정 구조

최상위 필드는 `schemaVersion: 1`, `kind: "reproof-protected-service"`, `profiles`다.
profile은 1~128개이며 profile ID와 프로젝트 ID는 중복되지 않아야 한다.
아래에 없는 필드는 거절한다. 키·비밀번호·명령·모듈·callback·qualification
기록을 전달하는 형식이 아니다.

| profile 필드 | 내용 |
| --- | --- |
| `id`, `projectId`, `projectDigest` | 선택 profile과 정확한 프로젝트 revision |
| `applicationId`, `originalBuildId`, `platform`, `deviceId` | 앱·선택 원본 빌드·플랫폼·기기 |
| `runtimePolicyDigest` | 이슈 runtime 정책 digest |
| `build` | `bundlePath` 또는 `hostPath`, 고정 실행 `route`, `journal` |
| `signing` | `toolsPath`, `toolsManifestSha256`, 공개 `definition` 참조, `policy`, `journal`, `ownerRoot` |
| `mobile` | 공개 `definition` 참조, 고정 실행 `route`, `journal`, `ownerRoot` |
| `validation` | 독립 검사 `plan`, 공개 `observers` 참조 |

공개 파일 참조는 정확히 `{ "path": "/absolute/public.json", "sha256": "…" }`다.
이 단계는 경로 형식과 digest 형식을 검사하며 참조 파일의 내용·존재를 확인하지 않는다.
`journal`은 `root`, `environmentDigest`, `diskBudgetBytes`다. 경로는 명시적 절대
경로이며 `..` 같은 별칭을 허용하지 않는다. 작업 root끼리 또는 공개 입력 root와
겹치는 경로도 거절한다. 실제 파일 소유권·inode·내용 검사는 후속 고정 실행기가 맡는다.

`route`, `policy`, `plan`은 기존 `execution.protocol` 계약을 그대로 사용한다.
build는 `build-guest` 또는 `host-build`, mobile은 `mobile-device`여야 한다.
프로젝트·앱·플랫폼·서명 정책 ID·검사 계획 ID와 각 실행 환경이 일치해야 한다.
서명 저널은 별도의 환경 digest를 가지며 실제 키 scope와 연결하는 절차가 추가로 필요하다.

## 빌드 격리 선택: `build-guest`(기본)와 `host-build`(명시적 opt-in)

`build-guest`는 봉인된 macOS VM(`bundlePath`)에서 후보를 빌드한다. 네트워크 없음,
호스트 파일시스템 격리, 오버레이 폐기가 probe로 측정된다.

`host-build`는 **격리 없이** 이 Mac의 고정 도구로 빌드하는 명시적 선택이다.
`bundlePath` 대신 `hostPath`에 host 번들 manifest를 지정하고 route의
`executionClass`를 `host-build`로 둔다. 둘을 섞으면(예: `hostPath`에 `build-guest`
route) 설정이 거절된다. host 번들은 실행할 도구의 절대 경로·SHA-256·크기와 고정
recipe 카탈로그를 선언하며, 매 qualification·실행 전에 실제 바이너리를 다시
측정한다. 스크립트 도구는 shebang 인터프리터도 고정 도구로 선언돼야 한다.
카탈로그에는 `host-qualification-probe` recipe가 반드시 있어야 한다.

host-build가 측정하는 것은 세 가지뿐이다: 선언된 toolchain이 변하지 않았는지,
취소 시 프로세스 그룹이 완전히 종료·수거되는지, run 디렉터리가 비워지고 예약이
해제되는지다. **보안 경계가 아니다** — 후보 코드는 일반 호스트 네트워크와
파일시스템에 접근한다. 따라서 이 모드의 결과물에는 `buildIsolation: "host"`가
표시되고(`TrustedBuildProof.public()`, tool inputs 공개 요약, qualification
보고서의 `isolation`), guest-VM 증명으로 사용할 수 없다. 회사가 AI 생성 코드를
리뷰 없이 빌드한다면 `build-guest`를 유지해야 한다.

### host-build의 알려진 한계

- **복구**: 호스트 run 디렉터리는 같은 UID의 후보 프로세스가 쓸 수 있어
  `termination.json` 같은 종료 증거를 신뢰할 수 없다. `reconcile`은 항상 거절되고
  (저널 scope가 `'host'`로 기록돼 VM 종료-기록 복구가 구조적으로 불가하다),
  quarantine 해제는 운영자가 저널을 직접 재설정해야 한다.
- **신호 의미**: host 모드의 `stopped`/`cleanupConfirmed`는 "관측된 프로세스 그룹이
  종료·정리됐다"는 운영 신호이지 무결성 증명이 아니다 — 같은 UID의 후보는 저널
  디렉터리를 직접 조작할 수 있다.
- **자손 프로세스**: 종료는 프로세스 그룹 SIGKILL 기반이다. 후보가 `setsid()`로
  그룹을 벗어난 자손은 추적·종료되지 않는다. 종료가 10초 안에 확인되지 않으면
  run은 격리된다.
- **toolchain TOCTOU**: 매 실행 직전에 도구 바이너리를 다시 측정해 창을 좁히지만,
  측정과 exec 사이의 경합을 완전히 없애지는 못한다. 완전한 결합은 보호된
  toolchain 디렉터리나 VM 격리가 필요하다.
- **리소스 필드는 예약·권고 값이다**: `cpuCount`·`memoryMiB`는 메타데이터일 뿐
  커널 수준에서 강제되지 않고, `diskBytes`는 저널 예약일 뿐 후보의 임의 쓰기를
  제한하지 않는다.
- **진단**: 후보의 stdout/stderr는 수집하지 않는다(후보 출력이 운영 로그로 새는
  것을 막기 위함). 실패는 구조화된 reason 코드로만 보고된다.

## 이슈와 실제 runtime의 연결

이슈 설정의 `repair.protectedProfileId`가 모든 보호 profile을 정확히 선택해야 한다.
빠진 profile, 사용하지 않는 profile, 다른 build recipe·검사 recipe·runtime 정책을
거절한다. 이슈 설정은 기존 `load_issue_configuration()` 계약도 통과해야 한다.

로컬 Python 조합에서는 `load_protected_service_configuration(path)`로 읽고,
`validate_issue_configuration(document)`를 서비스 생성 전에 호출한다.
`compose_issue_workflow(..., defer_repairs=True)` 이후 `validate_runtime(bundle)`는
실제 서비스의 프로젝트 등록 객체·runner·정책·기기 배정과 선택한 원본 빌드를
다시 확인한다. 같은 앱의 다른 등록 빌드도 선택 원본을 대신할 수 없다.
이 검사는 기기 lease나 서명·실행 권한을 반환하지 않는다.

## 구현과 검증 범위

읽기 전용 설정 검사 CLI와 실제 서비스 객체의 metadata 검사를 구현했다.
관련 검사는 합성 프로젝트·loopback fixture·합성 기기를 사용하며 프로세스나
기기를 시작하지 않는 조건, 잘못된 연결·닫힌 서비스·다른 앱·다른 원본을 확인한다.

[빌드·서명 입력 로더](PROTECTED-SIGNING-INPUTS.md)는 preflight 이후 명시적으로
참조 내용을 읽는다. `check-config` 명령 자체는 계속 참조를 열지 않는다.
`live-serve`에서 고정 VM·서명·관찰자·기기 실행기를 조합하는 시작 경로는
아직 남아 있다. mobile qualification runner/native guardian,
실제 회사·VM·Apple 서명·물리 기기·두 Mac 수용도 별도 완료 조건이다.
현재 설정 검사의 성공을 보호 서비스 준비 완료로 사용할 수 없다.

[Android mobile 입력](PROTECTED-MOBILE-INPUTS.md)과
[인증된 로컬 독립 관찰자](LOCAL-VALIDATION-PROTOCOL.md)의 본문 로더 및 factory 연결을
추가했다. 이들 역시 실제 mobile qualification과 전체 서비스 시작을 대신하지 않는다.

## 인증된 Android 복구 서비스

다음 명령은 설정에 고정된 Android 복구 프로필만 등록한다. `--shared-config`와
`--issue-config`가 모두 필요하며 별도의 Android/Simulator/iPhone 선택 옵션을 섞지 않는다.
기기 ID·앱·serial은 고정된 참조 digest와 프로젝트 메타데이터에서 확인하며 기본 ADB를
탐색하지 않는다. 이 프로세스의 기기는 일반 세션·기록 실행에 할당하지 않는다.

```sh
reproof live-serve --shared-config "$SHARED_CONFIG" --issue-config "$ISSUE_CONFIG" \
  --protected-recovery-config "$PROTECTED_SERVICE_CONFIG" --output "$SAME_LIVE_OUTPUT"
```

원래 서비스와 같은 output·authority·mobile journal·owner 경로 및 정확한
앱/기기 정의를 사용한다. 시작 시 실제 등록 객체와 고정 입력을 확인하며 새 journal을
대체 생성하지 않는다. 이 모드는 보호된 repair 실행기 조합을 보류하고 복구를 제공한다.

CLI는 현재 서비스에 인증하고 공개 ID와 원래 요청 digest만 보낸다. 자격 증명은 대화형
비밀 입력 또는 `--credential-stdin`으로 받으며 인자·출력에 넣지 않는다.

```sh
reproof protected-service android-profiles --server "$REPRO_SERVER"
reproof protected-service android-operations --server "$REPRO_SERVER" --profile "$PROFILE_ID"
reproof protected-service android-status --server "$REPRO_SERVER" \
  --profile "$PROFILE_ID" --operation "$OPERATION_ID"
reproof protected-service android-recover --server "$REPRO_SERVER" \
  --profile "$PROFILE_ID" --operation "$OPERATION_ID" \
  --request-digest "$REQUEST_DIGEST" --request-id "$RECOVERY_REQUEST_ID" \
  --timeout-seconds 120 --wait
reproof protected-service recovery-job --server "$REPRO_SERVER" --id "$RECOVERY_JOB_ID"
reproof protected-service recovery-cancel --server "$REPRO_SERVER" --id "$RECOVERY_JOB_ID"
```

조회에는 배정된 기기의 읽기 권한이 필요하다. 실행/취소는 현재 프로젝트의
`device.operate`와 `fixture.execute`를 모두 요구하며 원래 입력/작업을 다시 확인한다.
현재 host가 새 project grant를 발급하고 실제 DeviceAuthority와 최종 cleanup capability를
사용한다. 설정 JSON은 이 권한을 대신하지 않는다. 실행 중 자격·브라우저 세션·멤버십·기기
배정이 취소되면 다음 효과를 막고 불확실한 정리는 격리 상태로 유지한다.

복구는 최대 4개, 기기별 1개 작업으로 제한한다. 같은 request ID/동일 요청은 같은 작업을
반환하고 변경된 요청은 거절한다. HTTP는 202로 작업 ID를 반환하며 CLI `--wait`가 결과를
조회한다. 작업 조회 이력은 프로세스 내 128개로 제한되고 원래 operation journal은 남는다.
서비스 재시작 뒤에는 원래 작업을 다시 조회하고 새 request ID로 재개한다.

활성 세션·살아 있는 retained owner를 빼앗지 않는다. 종료된 원래 owner의 scope만 연결하고,
정리가 확인된 뒤 Lab의 예약을 반납한다. 등록된 보호 기기의 일반 `devices/.../recover`
경로는 원래 operation을 지정하도록 거절한다. CLI의 복구 성공은 원래 run의 실패/취소와
예약 해제를 뜻하며 앱 수정 검증 성공이 아니다.

실제 `live-serve` CLI 프로세스·인증된 HTTP·cached SDK/native guardian과 자체 protocol server를
연결해 검사했다. SDK 실행 중, staging 삭제 후, RunStore 예약 해제 commit 직후에 서비스에
SIGKILL을 보내고 새 프로세스에서 원래 작업을 복구했다. 뒤의 두 경우는 기기 명령을 반복하지
않았다. 실제 물리 기기 qualification 및 보호 build/signing/검증 실행기를 함께 여는 조합은
여전히 별도 완료 조건이다.

## 현재 권한을 사용하는 실행기 조합

신뢰된 로컬 Python 시작 코드에서 `load_protected_service_inputs()`는 고정 build/signing/mobile/
observer 입력을 묶는다. 이 단계는 VM·서명 키·기기 owner를 시작하지 않으며 결과는
`executionAuthority: "none"`이다. 이후 `compose_android_protected_service()`는 기존 이슈
runtime에, `compose_android_protected_workflow()`는 새 이슈 runtime에 실행기 전체를 연결한다.

조합에는 같은 `ProtectedRepairComposition.authority`가 실제 발급한 현재 mobile qualification,
명시적으로 등록된 `AndroidSigningMaterialResolver`와 `ValidationSecretRegistry`가 필요하다.
qualification 복사본·다른 issuer·폐기·만료·범위 불일치와 등록되지 않은 재료는 owner 생성 전에
거절한다. 설정이나 과거 probe 보고서를 qualification으로 읽는 경로는 제공하지 않는다.

빌드는 고정 VM probe를 현재 authority에서 측정하고, 결과에 따라 고정 unsigned APK 검사,
JVM signing owner/독립 inspector, Android mobile owner/외부 observer를 순서대로 연결한다.
중간에 권한·observer 정의가 변하면 연결하지 않으며 이미 만든 owner를 취소·수집한다.
호출자는 부분 정리가 끝나지 않은 경우에도 원래 composition을 보유하고 close를 재시도한다.
복구 전용 inventory는 이 일반 실행기 조합에 사용할 수 없다.

이 조합은 Android 대상의 신뢰된 로컬 API다. 일반 실행을 여는 CLI에는 실제 mobile
qualification runner와 명시적 secret 등록 연결이 더 필요하다. 현재 조합 검사는 실제 고정
팩토리와 runtime을 사용하지만 VM/mobile qualification 및 build-input loading은 명시적인
대역이고, private key 서명 자체를 이 검사에서 실행하지 않는다. 개별 native 서명 검사는 별도로
유지하며 실제 환경 qualification이나 전체 회사 이슈 수용 완료로 표현하지 않는다.

## 명시적 시작 재료 스트림

`protected_service_materials.read_service_materials(prepared, stream)`은 운영자가 명시적으로
전달한 binary stream만 최대 256 KiB까지 읽는다. 환경 변수나 credential 파일을 찾지 않는다.
이 입력은 공개 service/issue 설정에 넣거나 로그·저장소에 보관하는 형식이 아니다.
Base64는 바이트 인코딩이며 암호화가 아니다.

```json
{
  "schemaVersion": 1,
  "kind": "protected-service-materials",
  "configurationDigest": "준비한 공개 설정의 digest",
  "signing": [{
    "profileId": "등록된-profile",
    "keystorePath": "/absolute/operator-owned.p12",
    "keyAlias": "등록할-alias",
    "storePasswordB64": "비밀번호의-base64"
  }],
  "validation": [{
    "profileId": "등록된-profile",
    "authenticationReferenceId": "observer-정의의-참조",
    "providerId": "observer-정의의-provider",
    "secretB64": "32~64바이트-key의-base64"
  }]
}
```

signing에는 선택적으로 `keyPasswordB64`를 넣는다. 전체 profile/observer 참조 집합, 중복,
추가 필드, 크기, 경로 형식과 인코딩을 확인한 뒤 registry에 등록한다. 프로젝트/서명 identity는
준비된 입력에서 결정하며 비밀 입력이 바꾸지 못한다. 등록 시에는 키 파일의 소유권·metadata만
확인하고 키 내용은 고정 signer가 실제 서명할 때 연다. 공개 artifact 경로 규칙은 그대로 유지한다.

`bind_service_materials()`에 전달한 bytearray와 디코딩한 가변 임시 버퍼는 성공·실패 모두에서
지운다. registry는 별도 사본을 보유하고 close 또는 조합 실패 시 지운다. Python의 불변
JSON/Base64 임시 값이나 호출자가 보유한 원래 stream까지 완전히 소거한다고 주장하지 않는다.
공개 결과는 digest·등록 개수·`executionAuthority: "none"`만 포함한다.

`compose_service_from_material_stream()`은 현재 qualification과 비어 있는 시작 owner를 먼저
검사한 뒤 스트림을 소비하고 고정 실행기 조합에 연결한다. 읽는 동안 qualification이 폐기되면
다시 검사하여 실행하지 않는다. 이 함수도 신뢰된 로컬 API이며 실제 mobile qualification
runner와 일반 실행 CLI 연결을 대신하지 않는다.
