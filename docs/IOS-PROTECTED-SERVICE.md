# iOS 보호 실행과 재시작 복구

`IOSTrustedMobileAdapter`는 후보 설치와 독립 검증, 같은 승인 원본으로 세 번의 G4 재생,
원본 복원·앱/fixture 초기화·파일 정리를 `ProtectedMobileSupervisor`에 연결한다.
일반 등록·권한·서명·외부 검증 계약을 유지하며, 살아 있는 qualification capability가
없으면 서비스 조합이 실행을 시작하지 않는다.

## 서비스 조합

`compose_ios_protected_service`와 `compose_ios_protected_workflow`는 등록된 iOS 입력,
실제 qualification authority 및 VM·서명·mobile qualification을 받는다.
빌드 단계는 봉인 VM(`build-guest`)이 기본이며, 운영자가 명시적으로 선택하는
`host-build`(비격리 고정 toolchain)도 지원한다 — 결과는 `buildIsolation: "host"`로
표시되고 격리 증명으로 쓸 수 없다. 자세한 계약은
[보호 서비스 설정](PROTECTED-SERVICE-CONFIGURATION.md)을 참고한다.
`load_protected_service_inputs`는 파일과 등록을 검증하는 준비 단계이며 실행 권한이 없다.
`compose_service_from_material_stream`은 동종 플랫폼 구성을 고정 factory에 연결한다.
Android/iOS 혼합 실행 구성은 거절한다.

공개 service config의 iOS mobile 정의에는 고정 devicectl/guardian/Xcode/template,
원본/helper IPA, fixture 준비와 [초기화 정책](IOS-SANITATION.md)의 path/SHA-256을 선언한다.
서명 자료는 별도 private stream에서 다음 필드만 받는다.

```text
signing: [{profileId, pkcs12Path, passwordB64}]
validation: [{profileId, authenticationReferenceId, providerId, secretB64}]
```

외부 검증기는 Unix socket·peer UID·HMAC·nonce·기한과 실제 iOS 입력에 묶인다.
패스워드/검증 secret은 공개 config나 CLI 인자로 넣지 않는다. 사용자 자료를 읽기 전에는
해당 운영 환경의 승인이 필요하며 예제/검사는 자체 자료만 사용한다.

후보의 runtime profile은 `artifact.kind: "ios-ipa"`로 서명 증명의 IPA 바이트 SHA와
크기를 사용한다. 기존 `ios-app`은 tree manifest digest·크기를 유지한다. 서로 다른
digest를 매핑 JSON이나 검증 완화로 대체하지 않는다.

## 실기기 mobile-device qualification

`reproloop.ios_device_qualification.qualify_ios_device(backend_id, authority, subject, *,
environment_digest, signing_policy_id, expected_udid, ttl_ms, timeout_seconds, cancellation)`는
선택한 paired iPhone을 같은 `QualificationAuthority` 안에서 측정하고, 5개 probe가 모두 통과할 때만
`mobile-device` qualification을 발급한다. `PhysicalIOSProbeSubject(public_id, products)`는 고정
devicectl과 로컬 서명된 helper host/runner 산출물로 측정한다. 저장된 JSON·helper 응답·XCTest 요약은
capability를 만들지 못하며, 새 측정은 먼저 이전 qualification을 폐기한다.

| probe | 실제 관측 |
| --- | --- |
| `device-boundary` | paired·wired·developer mode·tunnel 연결, 등록 UDID 일치, 사설 IPv6 ULA tunnel, 잠금 해제(`passcodeRequired: false`) |
| `network-boundary` | USB tunnel ULA에만 listen, wired transport, 토큰 없음/오류 토큰 401, 인증된 `/status`, helper가 보고한 모든 비루프백 인터페이스 주소로의 호스트 TCP 연결 실패 |
| `backend-scope` | 렌더링한 host/helper/provider incarnation 일치, 외부 provider incarnation `/activate` 거절, 정상 `/activate` 성공, 다른 provider incarnation 명령 거절, `/retire` 202, 이후 일반 명령 거절 |
| `process-termination` | xcodebuild exit 0, XCTest 1 통과·0 skip·0 실패, helper host/runner 실행 파일 process 목록 부재 |
| `state-cleanup` | helper bundle 삭제 후 `device info apps` 부재, staged xctestrun/xcresult 제거 |

2026-09-16 QA-iPhone(iPhone 17 Pro, iOS 27.0) 실측 결과는
[device-qualification-r1.json](../artifacts/product-delivery/d4-ios-device-qualification-r1/device-qualification-r1.json)이다.
잠긴 기기에서는 XCTest가 180초 예산을 넘겨 `runValid: false`로 차단됐고, 이 관측이 `unlocked` 조건의 근거다.
`network-boundary`는 helper `/status`의 `networkInterfaces`(비루프백·UP 인터페이스의 IPv4/IPv6 주소)를
사용해, tunnel 주소를 제외한 모든 기기 주소로 호스트에서 helper 포트로의 TCP 연결을 시도한다. r1
재측정에서 helper는 32개 주소를 보고했고 하나도 도달하지 못했다(`nonTunnelInterfaceCount: 32`,
`nonTunnelReachableCount: 0`). `backend-scope`는 외부 provider incarnation으로 `/activate`가 거절됨을
함께 측정한다.

cleanup 단계의 schema-2 영수증·동시 writer 부재는 이 XCTest probe 경로가 아니라
호스트측 저널 레이어에서 별도 실측한다 — 2026-09-16 실제 QA-iPhone UDID의 물리 scope claim에
묶인 진짜 `IOSMobileOperationStore` 생명주기로 44개 probe를 측정했고 전부 통과했다
([native-cleanup-measurement.json](../artifacts/product-delivery/d4-ios-device-qualification-r1/native-cleanup-measurement.json)):
schema-2 `native.json` 지속 기록, 동시 claim/owner/prepare/callback 거절,
producer.lock·device lease의 커널 flock 보유(별개 프로세스/디스크립터로 확인),
`native-finalization/` 의도·상태 `discarded`·evidenceDigest, 역할별 staged 파일 제거,
`admit` 종료 후 잠금 해제. 대상 앱 자체의 네트워크 트래픽 격리(egress 정책)는 여전히 미측정이며,
cleanup의 기기 dispatch 구간(helper cleanup 명령·sanitation 관측·hold 소비)은 서비스 활성화가
필요하다. 실제 앱 Keychain 초기화는 별도 검증 조건으로 남는다.

## 정상 종료와 복구

정상 흐름은 하나의 retained owner 아래에서 설치 → replay 1..3 → cleanup을 실행한다.
검증기 실패로 replay에 진입하지 못한 경우에도 정확한 기존 owner로 cleanup에 진입한다.
각 replay가 끝날 때 대상 앱 초기화·종료와 helper/호스트 수거를 확인한다. 마지막에는
원본 앱을 복원하고 새 helper로 원본의 시작/종료 초기화를 확인한다. 알려진 큰 앱·IPA·
XCTest 산출물을 정리한 뒤에만 파일 hold와 실행 예약을 반환한다. 작은 저널은 보존한다.

native 작업이 중단되면 `IOSNativeRecoveryContext`가 원래 operation/producer와 실제
`DeviceAuthority`의 recovery lease를 다시 묶는다. 디스크의 context JSON은 입력으로만
읽으며, 일반 `MobileContext` 권한을 재구성하지 않는다.

복구는 다음 순서를 따른다.

1. 실제 고정 기기의 전체 process 관측에서 이전 helper의 실행 여부를 확인한다.
   살아 있으면 저장된 고정 launch credential과 **현재** 기기 tunnel을 대조하고,
   새로운 복구 권한으로 `/retire`를 호출한다. queued 입력을 버리고 일반 입력/실행을 막는다.
   성공 ACK와 별도로 새 process 관측에서 helper executable의 부재를 확인한다.
2. 새 원본/helper 사본과 새 helper incarnation으로 원본 설치·시작/종료 초기화를 수행한다.
   복구는 `restore-original`, `xctest-original`과 고정 cleanup만 발급한다.
3. 해당 원본 replay의 정확한 fixture 할당을 복구·정리한다. 현재 실행 중인 issue나
   바뀐 할당/세대는 정리 대상으로 추정하지 않는다.
4. 실제 발급된 관측으로 reconciliation을 하고, 원래 잠금을 유지하며 큰 파일과 hold를
   정리한다. cleanup pending 상태와 예약이 끝나야 기기를 다시 사용할 수 있다.

새 helper 작업은 최대 세 번이다. 이전 시도의 큰 사본을 정리한 후 다음 시도를 준비하며,
원래 admission은 복구 앱 사본·공유 archive·세 번의 출력 상한까지 예약한다.
추출은 기록된 작업 디렉터리 안에서만 수행한다. 부분 archive는 등록된 바이트의 prefix를,
부분 앱은 원본 IPA에서 도출한 남은 파일 집합·타입·권한·크기·내용을 대조해 재개한다.
모르는 파일을 지우거나 이전 permit을 복원하지 않는다.
기한/취소, 알 수 없는 자식·파일, 해석할 수 없는 process 목록, helper 부재 확인 실패는
격리를 유지한다. 저장된 종료 기록만으로 새 복구 증명을 발급하지 않는다.

## CLI

```text
reproloop protected-service check-config --config /absolute/service.json --issue-config /absolute/issues.json
reproloop live-serve --shared-config /absolute/shared.json --issue-config /absolute/issues.json \
  --protected-recovery-config /absolute/service.json --output /absolute/output
reproloop protected-service ios-status --server <configured-origin> --credential-stdin \
  --profile <profile-id> --operation <original-operation-id>
reproloop protected-service ios-recover --server <configured-origin> --credential-stdin \
  --profile <profile-id> --operation <original-operation-id> --request-digest <sha256> --wait
```

복구 서비스는 로그인한 현재 principal·프로젝트·기기·fixture 권한을 매 경계에서 확인한다.
`ios-profiles`, `ios-operations`, `recovery-job`, `recovery-cancel`도 같은 인증 경로를 사용한다.
`ios-mobile recover`는 계속 준비 전용이며 native 작업을 우회 해제하지 못한다.

정상 보호 실행의 외부 진입점은 살아 있는 qualification을 받는 service factory다.
CLI flag나 JSON으로 qualification을 만들어 실행하는 경로는 제공하지 않는다.
실기기 환경의 filesystem/SDK daemon/helper·기기 네트워크/backend 격리 측정,
실제 Apple 서명/Keychain, 회사 앱과 두 Mac 수용은 현지 환경에서 수행해야 한다.
자체 SDK/HTTP 대역과 Simulator 통과는 이 실제 수용을 대신하지 않는다.
