# 고정 iOS 실행과 재생 연결

이 API는 살아 있는 native owner 안에서 고정된 설치·XCTest·helper·앱 식별 관측을 연결한다.
`ProtectedMobileSupervisor`의 trusted iOS adapter·native 복구·서비스 조합은
[보호 서비스](IOS-PROTECTED-SERVICE.md)를 따른다. [앱 초기화 정책](IOS-SANITATION.md)은
명시적으로 구성하며 실제 환경 qualification이 있어야 보호 검증을 승인할 수 있다.

## 입력과 실행 순서

`IOSMobileInputsConfig.xctest`는 선택 항목이다. 등록 JSON에서는 `xcodebuildPath`,
`xcodebuildSha256`, `developerRoot`, `template: {path, sha256}`와 선택적인 `port`를 받는다.
같은 고정 guardian과 helper host/runner IPA가 필요하다. 생성 템플릿의 runner bundle ID와
등록된 helper runner가 일치해야 한다. 로더는 실행 파일과 archive를 대조하며 프로세스나 기기를 시작하지 않는다.

1. `IOSMobileOperationStore.admit()` 안에서 candidate/original/helper-host/helper-runner를 준비한다.
2. 실제 발급된 기기 authority로 `native_owner()`에 진입한다. 이후 준비 전용 복구로 돌아갈 수 없다.
3. 고정 설치 permit으로 candidate를 설치하고 `observe_installed()`로 설치 bundle/version/build를 대조한다.
4. `IOSXCTestRunner.prepare()`로 회차별 생성 템플릿과 새 실행 UUID를 고정한다.
5. `Lab.bind_retained_startup()`으로 논리적인 세션 시작을 정확한 XCTest payload/provider 객체에 묶는다.
6. `IOSG4Provider.run_replay()`가 기존 `IssueSessionService.replay()`를 호출한다. Lab 작업 스레드의
   고정 요청은 `IOSOwnerCommandPump`를 통해 원래 native 콜백 스레드에서 실행한다.

시작 시 helper handshake와 설치 identity 확인 뒤 고정 앱 컨테이너에서 runtime marker를 읽는다.
이 관측과 첫 프레임 게시가 끝나야 시작 성공을 반환한다. 요청 큐는 상한과 기한이 있으며,
대기 중 취소된 요청은 실행하지 않고 실행 중 결과를 놓친 요청은 성공으로 게시하지 않는다.
SDK/helper 프레임은 geometry와 native clock mapping을 검증해 기존 Lab 프레임 경로로 전달한다.

## 서로 다른 식별자의 의미

| 값 | 의미 |
| --- | --- |
| IPA SHA-256 | 전송·보관한 압축 파일의 정확한 바이트 |
| `appDigest` | 파일·권한·디렉터리·코드 객체·embedded profile을 포함하는 앱 모델 |
| runtime profile의 `artifact.sha256` | `ios-app`은 tree manifest digest, `ios-ipa`는 압축 파일 SHA-256 |
| XCTest payload의 `profileDigest` | 선택한 `IosAppProfile` |
| `runtimeIdentity.profileDigest` | 앱에 내장된 `AutoRecordProfile` |
| `runtimeIdentity.runId` | 이번 실행에 새로 발급한 UUID |

위 digest들은 서로 대체할 수 없다. runtime marker의 build ID는 앱의 embedded `ReproBuildID`이며,
보호 검증의 후보 build label과 별개다. marker는 app-reported 관측이고 설치 바이너리 SHA 증명이 아니다.
종료됐거나 실패한 XCTest launch에서 새로운 runtime 관측을 만들 수 없다.

## 지원 경계

현재 G4 bridge는 native pixels와 선언된 좌표 입력을 대상으로 한다. locator·semantic observation과
일반 재실행 명령은 지원하지 않는 동작으로 거절한다. HTTP helper 응답이나 저장된 JSON은
qualification, dispatch permit, sanitation 또는 기기 소유권 해제 권한을 만들지 않는다.

helper 종료와 호스트 프로세스 수거, 대상 앱의 종료 관측, 앱/서버 데이터 초기화는 별개의 결과다.
`authority_cleanup`의 성공 응답은 현재 명령 권한 안에서 XCTest가 관측한
`cleanupEvidence: {bundleId, state: "not-running", observer: "xctest-application-state"}`를
포함해야 한다. 오래된 helper의 관측 없는 응답이나 다른 명령의 관측을 사용하지 않는다.
디스크에는 관측 digest를 남기며 저장된 JSON만으로 새 종료 관측을 발급하지 않는다.
원본을 다시 설치한 것만으로 데이터 초기화를 완료했다고 표시하지 않는다. 알 수 없는 native 작업은
원래 잠금·작업 기록·IPA·예약을 보존하고 후속 native 복구를 요구한다.

실제 unsigned Xcode helper의 Simulator 실행과 기기용 빌드 파싱은 로컬 호환성 증거다.
물리 iPhone의 설치/copy/네트워크·daemon 격리, 회사 앱과 두 Mac 수용은 별도로 검증해야 한다.
