# 고정 Android mobile 입력

2026-09-14 · D4 진행 중

`load_protected_mobile_inputs(configuration, issue_configuration, runtime_bundle)`는
공개 설정과 실제 서비스 등록을 대조한 뒤 `mobile.definition`을 읽는다.
로컬 `android-live` 기기만 받으며 원격 기기를 로컬 ADB 대상으로 취급하지 않는다.
입력 읽기 전후에 같은 등록·원본·기기 배정을 확인한다. ADB·fixture 작업·기기 예약이나
qualification을 시작하지 않는다.

정의의 필수 필드는 다음과 같다. 추가 필드는 거절한다.

`adbEndpoint`는 선택 필드이며 [명시적인 ADB 연결](SCOPED-ADB-TRANSPORT.md)의
socket·버전·sandbox 해시를 담는다. 선택하면 작업 저널에도 바인딩된다.

| 필드 | 형식 |
| --- | --- |
| `schemaVersion`, `kind` | `1`, `android-mobile-definition-v1` |
| `owner`, `serial` | 명시적 소유자 ID와 등록된 로컬 기기의 정확한 serial |
| `runtimeProfile` | `{path, sha256}` JSON 참조 |
| `originalApk`, `helperApk` | `{path, sha256, bytes}` APK 참조 |
| `tools` | `adbPath`, `adbSha256`, `packageInspectorPath`, `packageInspectorSha256` |
| `preparations` | `{fixtureId, payloadDigest}` 배열 |

JSON은 최대 1 MiB이며 참조의 원시 파일 SHA-256을 대조한다. 원본 profile은 기존
`AndroidRuntimeProfile` 계약과 정확한 `originalBuildId`·APK 해시/크기를 만족해야 한다.
helper와 도구의 실제 바이트도 검사한다. fixture payload는 정의 파일에서 받지 않고
등록된 runtime에서 복사한다. 이후 payload·plan·파일 변경은 `verify()`에서 거절한다.

Android의 `PreparedMobileInputs.profile(id).config`는 기존 `AndroidMobileAdapterConfig`다.
공개 요약에는 경로·serial·payload를 넣지 않고 `executionAuthority: "none"`을 명시한다.
이 config를 실제 mobile supervisor에 쓰려면 같은 authority가 측정한 qualification이
별도로 필요하다. iOS 보호 mobile adapter는 아래 별도 고정 iOS 경로로 구성한다.

## iOS 등록 입력과 준비 저장소

iOS는 같은 loader에서 `IOSMobileInputsConfig`를 반환한다. 정의 JSON의 필수 필드는
`schemaVersion: 1`, `kind: "ios-mobile-definition-v1"`, `owner`, `udid`,
`coreDeviceIdentifier`, `runtimeProfile`, `query`, `baselines`, `preparations`다.
`runtimeProfile`은 path/SHA-256 참조다. `query`는 실제 Mach-O의 `devicectlPath`와
`devicectlSha256`, 독립적인 `workRoot`를 명시한다. 이 선언은 디렉터리나 프로세스를 만들지 않는다.
선택 필드 `query.nativeGuardian: {path, sha256}`는 고정 네이티브 조회 감시기를 지정한다.
감시기의 경로·해시를 조회 정의에 포함하고 work root와의 중첩도 거절한다. 이 정의로 조회하려면
모든 IPA를 준비한 원래 작업의 native owner를 `open_client(native_owner=...)`에 전달해야 한다.

`baselines`의 각 항목은 `role`, `bundleId`, `archive: {path, sha256, bytes}`다.
`original`을 필수로 받고, helper를 포함할 때는 `helper-host`와 `helper-runner` 두 역할을 함께
지정한다. bundle은 서로 달라야 한다. 한 프로필의 baseline IPA 총합은 64 MiB 이하다.
조회 작업 root는 journal/owner/tool/input 경로와 겹치면 거절하며 다른 프로필도 함께 확인한다.

loader는 실제 등록된 project/application/build, 기기 할당, profile/identity, physical UDID,
fixture 준비 정의를 대조한다. IPA의 ZIP 구조·암호화·개수·확장 크기를 검사하고 파일 내용을
스트리밍해 기존 `tree_manifest`와 같은 앱 해시·크기를 계산한다. `.DS_Store`를 제외하는 기존
프로필 의미는 유지하며, IPA 전체는 별도의 SHA-256으로 고정한다. 원본의 bundle/version/build가
등록 프로필과 맞아야 하고, root Info.plist의 package type은 `APPL`이어야 한다.
실행 파일 이름도 유효한 앱 내 파일이어야 한다. 앱을 디스크에 풀거나 서명·기기 도구를 실행하지 않는다.

`PreparedMobileInputs.open_ios_preparation(profile_id, configuration, issue_configuration, runtime_bundle)`는
현재 등록·파일을 다시 검증한 뒤 해당 프로필에 지정된 journal과 owner root만 연다.
`config.read_baselines()`가 반환한 고정 `BlobSet`과 `config.definition`을 기존
[영속 준비·복구 API](IOS-MOBILE-OPERATIONS.md)에 연결한다. helper 프로토콜·서명·실제 기기 상태를
검증했다는 뜻은 아니다. 원래 source가 없는 복구에는 이미 알려진 정의로 복구 저장소를 여는
기존 API를 사용한다. 이 factory의 `create=False`도 현재 입력 검증을 생략하지 않는다.

iOS는 고정 조회·설치·XCTest, runtime identity와 명시적 `sanitation: {path, sha256}` 정책,
trusted mobile adapter·독립 Unix 관찰자·native 복구를 제공한다. `xctest`와 sanitation은
보호 iOS 서비스에서 필수다. 세부 조합·인증 CLI·실환경 검증 조건은
[IOS-PROTECTED-SERVICE.md](IOS-PROTECTED-SERVICE.md)를 따른다.

고정 독립 관찰자의 입력·프로토콜은 [로컬 검증 프로토콜](LOCAL-VALIDATION-PROTOCOL.md)을
따른다. factory의 `validation_inputs`와 `validation_secrets`를 함께 전달하면 내부에서
생성한 실제 Android 어댑터에 관찰자를 연결한다. 기존 `validators` 인자와 동시에
전달할 수 없다. 서비스 종료는 관찰자 인증키의 진행 중 사용을 기다리고 원본·사본을
폐기하며, 불명확한 종료는 재시도 가능한 소유자를 유지한다.

검사는 명시적인 Android 도구/UI 대역과 실제 등록·scope·Unix 소켓을 사용했다.
실제 기기의 네트워크·backend 격리 qualification, native guardian과 전체 `live-serve`
시작 조합·회사·두 Mac 수용은 아직 남아 있다.
