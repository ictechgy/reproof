# iOS 앱 초기화 계약

`IOSMobileInputsConfig.sanitation`은 소유 앱의 초기화 범위를 명시하는 고정 정책이다.
보호 iOS 어댑터에는 이 정책과 XCTest helper 설정이 모두 필요하다. 회사 앱의 데이터나
Keychain을 자동으로 선택하지 않는다. 선언 가능한 형태는 다음과 같다.

```json
{
  "schemaVersion": 1,
  "kind": "ios-app-owned-sanitation",
  "paths": [{"root": "documents", "relativePath": "Drafts/Temporary"}],
  "userDefaultsKeys": ["test.draft"],
  "keychainGenericPasswords": []
}
```

경로 root는 `documents`, `application-support`, `caches`다. 상대 ASCII 경로만 받고
숨김·상위·절대 경로, 대소문자 및 조상 경로 중복을 거절한다. SDK의
`application-support/ReproLoop` 영역은 선택할 수 없다. 경로 최대 64개,
현재 앱의 standard UserDefaults 키 최대 128개, Keychain 항목 최대 32개,
정책 JSON 최대 32 KiB이며 적어도 하나의 선택이 필요하다.

Keychain은 `keychainGenericPasswords: [{"service": "...", "account": "..."}]`로
정확한 nonsynchronizable generic-password 항목만 지정한다. 실제 서명 entitlement의
명시적인 keychain group이 하나여야 한다. 항목 속성만 조회하고 값은 읽지 않는다.
복수 일치나 지원하지 않는 entitlement는 삭제 전에 거절한다.

`prepare_ios_instrumentation(..., sanitation_policy=policy)`가 JSON·digest를 Swift와
Info.plist에 함께 넣는다. `ReproRuntimeIdentitySchemaVersion`은 정책이 있으면 2,
없으면 기존 1이다. 정책을 포함한 원본·후보의 digest가 등록 입력과 일치해야 한다.
실행 시에는 정책 내용을 주입하지 않고 `REPRO_SANITATION_POLICY_DIGEST`만 전달한다.

초기화는 앱 bootstrap에서 동기적으로 실행된다. 경로 삭제, defaults/keychain 삭제 후
부재를 재검사한다. 실패하면 정상 시작 전에 종료하며 성공 영수증을 만들지 않는다.
시작 marker의 schema 2는 기존 identity 필드에 `sanitation`만 추가한다. 영수증은
정책·실행 UUID·`launch`/`cleanup` 단계·시간·선택 수·완료 상태를 묶는다.
호스트는 새 launch에 속하는 정확한 marker만 사용한다. 시작 시 파일이 아직 없거나
이전 UUID이면, 매번 SDK 프로세스와 복사본을 정리하면서 최대 32회/10초 안에서 재시도한다.

종료는 고정 Darwin notification으로 앱의 main thread에서 다시 초기화·재검사하고
cleanup marker를 fsync한 뒤 종료한다. XCTest의 `not-running` 관측, cleanup 영수증,
helper 종료, 호스트 그룹 수거는 각각 확인한다. 원본 재설치만으로 완료 처리하지 않는다.

이 영수증의 등급은 `app-self-verified-sanitation`이다. 기기 전체의 초기화를 증명하지 않는다.
선택한 저장소에는 background task, 더 이른 전역 초기화, extension, 다른 프로세스가
동시에 쓰지 않아야 한다. App Groups/iCloud/WebKit 및 열린 SQLite/CoreData 저장소 등의
추가 writer를 이 구현이 자동 탐지하지 않는다. 이 앱 동작과 기기 경계는 실환경 검증 조건이다.

자체 Simulator 앱에서는 경로/defaults의 시작·종료 초기화, 실패 시 정상 시작 차단과
marker를 검증했다. unsigned Simulator 앱은 entitlement 오류가 나고 ad-hoc entitlement 앱은 실행이 거절되어,
그 실행을 Keychain 통과 근거로 사용하지 않는다.

실제 서명된 fixture 앱의 물리 iPhone(QA-iPhone, iOS 27.0) 측정은
[keychain-device-validation.json](../artifacts/product-delivery/d4-ios-device-qualification-r1/keychain-device/keychain-device-validation.json)과
[keychain-cleanup.json](../artifacts/product-delivery/d4-ios-device-qualification-r1/keychain-device/keychain-cleanup.json)에 있다.
단일 keychain-access-groups entitlement를 가진 서명 앱에서 선택 항목은 bootstrap 초기화로 삭제되고
비선택 항목은 유지됐다(-25300/0). 비선택 항목은 **앱 삭제·재설치에도 유지**됐으며 두 항목을 모두 선택한
정책 변형의 실행으로만 제거됐다 — 앱 재설치를 Keychain 초기화로 취급하면 안 된다.
probe 파일 회수는 일부 실행에서 시차로 실패했고, 최종 상태는 원본 앱 재설치·probe로 확인했다.
회사 앱의 실제 항목 선택·삭제와 cleanup 영수증의 동시 writer 부재는 여전히 실환경 수용 조건이다.
