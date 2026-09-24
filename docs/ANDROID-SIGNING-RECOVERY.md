# Android 서명 작업의 중단·복구 계약

2026-09-13 · D4 개발 API/CLI 구현. 새 배포 wheel 검증은 진행 중이다.

`ProtectedRepairComposition.configure_android_signing_owner()`가 고정 JVM 서명·독립
검사기와 `SigningOperationStore`를 서비스에 연결한다. 재시작 뒤에는 이전 Python
객체가 사라지므로 PID나 `termination.json`만으로 서명 비용을 해제하지 않는다.
이전 `configure_android_signing()` 경로에는 아래 영속 소유자 복구 계약이 적용되지 않는다.

## 네이티브 소유자

`native/android-signing-owner`의 고정 JVM은 apksig를 프로세스 안에서 실행한다.
외부 Java·aapt2 자식을 실행하지 않는다. 부모가 미리 잠근 파일 descriptor를 넘기고
JVM은 그 잠금을 OS 프로세스 종료까지 유지한다. 별도 pipe의 부모 종료 신호는 JVM의
중단을 유도한다. 정상 반환도 결과를 읽은 부모와 종료를 조정하며 잠금을 먼저 풀지 않는다.

시작·종료 의향 기록은 operation/request/context/scope/실행기 definition digest를
포함한다. **종료 의향 기록은 프로세스 종료 증명이 아니다.** 원래 inode의 잠금을
다시 얻고 소유 작업 디렉터리를 확인한 뒤에만 종료를 확인한다. 소유자가 살아 있거나
파일이 바뀌었으면 PID에 신호를 보내지 않고 quarantine을 유지한다.

## 영속 순서

1. `TrustedSigningSupervisor`가 canonical 서명 scope lease를 얻는다. 같은 인증서를
   다른 참조 ID나 작업 경로로 바꿔 이전 quarantine을 우회하지 못한다.
2. 아직 존재하지 않는 operation ID에 서명 작업 intent를 먼저 기록하고 fsync한다.
   기존 미확정 작업에 새 intent를 덧씌우는 마이그레이션은 허용하지 않는다.
3. RunStore가 요청 digest와 실제 작업 용량을 예약한다. 이 단계 전에 네이티브 도구를
   실행하지 않는다. intent 뒤 admission 전에 중단된 작업은 별도로 확인하는 orphan이다.
4. 감독자가 현재 작업과 context에 묶인 프로세스 내부 capability를 서명/검사기에 준다.
   context digest는 기존 의미를 유지하며 이 비직렬화 capability를 포함하지 않는다.
5. 콜백은 operation의 producer lock을 보유한다. 입력·출력 한도, 고정 실행기 digest,
   phase context와 허용 파일명을 기록한 뒤 phase별 잠금을 얻어 JVM에 상속한다.
   intent·파일 identity·시작 권한이 고정되기 전에 spawn하지 않는다.
6. 부모는 현재 취소·기한을 확인하고, 정상 결과 또는 중단 뒤 JVM 종료와 phase 잠금
   재획득을 확인한다. 허용된 private 작업 파일만 정리하고 phase 정리 기록을 fsync한다.
   종료가 불명확하면 원래 파일·잠금·비용을 유지한다.
7. 감독자는 반환된 콜백과 모든 phase의 정리를 확인한 뒤 RunStore를 완료한다.
   타임아웃 뒤 늦게 돌아온 콜백은 서명 proof를 발급할 수 없다.

producer lock은 JVM 종료 후에도 살아 있는 Python 콜백과 복구가 겹치는 것을 막는다.
콜백이 아직 살아 있으면 다른 프로세스의 복구가 그 작업을 정리했다고 선언하지 못한다.
재시작 시 producer lock은 해제돼도 상속된 JVM phase 잠금은 JVM 종료까지 남는다.

## 운영 복구

복구는 같은 scope의 canonical lease와 RunStore의 배타적 작업 lock을 얻는다. 원래
operation/request 바인딩과 private intent를 확인하고, producer lock 및 기록된 모든
phase 잠금을 얻는다. 아직 살아 있는 소유자에게는 기록된 PID로 신호를 보내지 않는다.

복구기는 각 phase의 원래 파일 identity, 고정 실행기 definition, 작업·context 바인딩,
허용 파일 집합과 실제 정리를 확인한다. 그동안 잠금을 유지한 채 한 번만 쓸 수 있는
프로세스 내부 cleanup capability를 발급한다. RunStore는 정확한 복구기 인스턴스와
scope·operation·request 바인딩을 검사한 뒤에만 실패/취소 상태로 비용을 해제한다.
복구는 빌드·서명·후보 재생을 재개하거나 검증 성공을 발급하지 않는다.

intent가 없는 과거 quarantine, 바뀐 inode, 알 수 없는 파일, 읽을 수 없는 기록,
살아 있는 producer/JVM은 확인되지 않은 상태로 남는다. 상태 출력에는 정적인 상태명과
공개 digest만 포함하고 키·비밀번호·token·PID·도구 출력·private 경로를 넣지 않는다.

## CLI 사용

고정 도구는 `reproof android-signing build-tools --help`의 명시적인 JDK·java·javac·jar·
clang·apksigner JAR 경로와 각 SHA-256을 받아 새 디렉터리에 만든다. 다운로드나
Keychain/키스토어 조회는 하지 않는다. 공개 배포 리소스의 Java/JNI 소스, 도구·헤더,
JAR의 중첩 클래스와 출력 digest를 검사한 후 원자적으로 게시한다. 취소·시간 초과·
출력 초과·Ctrl-C는 프로세스 수거를 확인하며, 종료가 불명확하면 작업 폴더를 보존한다.

상태 조회와 복구는 운영자가 기존 서명 소유자에서 기록한 공개 JSON을 받는다.
설정의 필드는 정확히 다음과 같다.

| 필드 | 값의 출처 |
| --- | --- |
| `schemaVersion`, `kind` | `1`, `android-signing-owner-v1` |
| `runStorePath`, `environmentDigest`, `diskBudgetBytes` | 원래 `RunStore`의 절대 경로·환경 digest·예산 |
| `ownerRoot` | 원래 `SigningOperationStore`의 절대 경로 |
| `toolsPath`, `toolsManifestSha256` | `build-tools`가 만든 경로와 반환한 `manifestDigest` |
| `definitionDigest` | 해당 `SigningOperationStore.definition_digest` |
| `identity` | `referenceId`, `applicationId`, `packageName`, `certificateSha256`, `signatureSchemes`, `permissions` |

`identity`에는 불투명한 재료 참조 ID와 공개 식별/제약만 넣는다. 키 경로·별칭·비밀번호·
자격증명·명령·콜백·qualification 값은 이 형식에서 받지 않는다. 예산은 입력/출력 각
64 MiB와 저널 512 KiB 이상이어야 한다. 경로나 참조 ID를 바꿔 과거 작업을 복구하지 않는다.

```sh
reproof android-signing status --config /absolute/signing-owner.json --operation OPERATION_ID
reproof android-signing recover --config /absolute/signing-owner.json --operation OPERATION_ID --request-digest REQUEST_SHA256
```

`status`는 기존 디렉터리만 열고 서명 도구를 실행하지 않는다. `producer-live`,
`recovery-required`, `recovery-sanitized`, `terminal`, `no-intent`, `intent-orphan` 중 관측한
상태를 출력한다. 상태 조회의 종료 코드 0은 조회가 끝났다는 뜻이며 실행/재사용 허가는 아니다.

`recover`는 원래 요청 digest를 요구한다. `SigningOperationStore.recovery()`의 잠금
범위 안에서 `RunStore.finish_signing_recovery()`가 정확한 일회성 capability를 소비한다.
성공한 복구 결과는 `failed` 또는 `cancelled`, 예약 바이트 0이며 후보 검증 성공이 아니다.
이미 끝난 작업은 원래 요청을 확인한 뒤 `already-terminal`로 보고한다. 잘못된 입력은
종료 코드 2와 정적인 오류를 반환하고, 예전 intent 없는 작업이나 살아 있는 소유자의
예약을 해제하지 않는다.

## 검증한 범위

실제 별도 프로세스의 `os._exit`로 intent, admission, phase 기록, spawn, native 시작,
서명 중, 결과 반환, 종료 의향 fsync, 부분 파일 정리, cleanup capability 발급, RunStore
완료 사이를 끊는 회귀 검사를 수행했다. 재시작 검사는 살아 있는 잠금과 죽은 소유자를 구분하고, 같은 키의
다른 상태 경로·변조된 JSON·잘못된 context·추가 파일·심볼릭 링크가 비용을 해제하지
못함을 확인했다. 상태 조회의 FIFO/경로 교체, 잘못된 설정, 추가/중복 필드와 대체 저널
생성 거절도 검사했다.

별도 [실제 CLI 실행](../artifacts/product-delivery/d4-protected-adapters-r1/signing-recovery/actual-cli-r1/result.json)은
자체 임시 키와 공개 Inventory APK로 실제 서명·독립 검사를 수행하고, 부모 Python을
종료한 뒤 새 CLI 프로세스에서 남은 134,742,016바이트 예약을 실패 상태로 해제했다.
test 모듈이나 VM/기기 대역을 이 실행에 가져오지 않았다. 각 JVM phase가 정리된 뒤
RunStore 완료 전에 종료한 사례이며, 모든 native 중단 지점을 이 단일 실행에서
다시 검사한 것은 아니다. 회사 키·VM qualification·보호된 후보 재생은 포함하지 않는다.
