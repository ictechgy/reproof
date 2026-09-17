# 명시적 iPhone 도구·기기 조회

`IOSDeviceTools`와 `PinnedDeviceCtlClient`는 관리자가 제공한 실제 실행 파일의 절대 경로·SHA-256,
CoreDevice UUID, 기기 UDID, 앱 bundle ID와 private 작업 디렉터리를 고정한다.
`select_iphone(query_client=client, ...)`는 이 클라이언트로 선택한 기기를 확인한다.
실패하거나 요청한 공개 기기 ID와 다르면 전역 기기 검색으로 넘어가지 않는다.

현재 제공하는 조회는 `details`, `apps`, `processes`다. 모두 고정 `device info` 명령과
정확한 `--device`를 사용한다. 앱 조회는 고정 `--bundle-id`를 추가하고, 다른 앱의 응답은
거절한다. 추가 조회 전에도 실제 details 응답의 UUID·UDID·iPhone/iOS 종류를 확인한다.
연결·pairing·설치·프로세스 종료·임의 명령을 이 API로 실행할 수 없다.

Xcode의 `usr/bin/devicectl` 셸 래퍼는 버전이 맞지 않으면 `xcodebuild -runFirstLaunch`를
실행할 수 있어 거절한다. 명시적으로 지정한 Mach-O 실행 파일을 직접 호출하며 매번
해시를 확인한다. 이번 Mac의 공개 SDK 파일 검사 근거는
[고정 도구 확인](../artifacts/product-delivery/d4-protected-adapters-r1/ios-device-tools-cached-binding-r1.json)에 있다.
도구를 실행하거나 CoreDevice 서비스·기기·pairing 파일을 조회한 결과는 아니다.

자식 프로세스는 별도 그룹과 고정 환경으로 실행한다. 결과 JSON은 256 KiB,
stdout/stderr는 각각 64 KiB로 읽기를 제한하며 실행에는 취소와 기한을 적용한다.
결과 파일의 크기·종류도 실행 중 감시한다. 이는 파일 시스템의 강제 디스크 quota가 아니다.
프로세스 수거가 불명확하거나 예상하지 않은 파일/링크가 남으면 작업 파일을 보존하고
추가 조회를 거절한다. `close()`의 `False`는 정리 완료가 아니며 이후 수거 확인과 재시도가 필요하다.
클라이언트를 닫기 전까지 호출자가 수명과 재시도를 관리한다.

공개 관찰에는 digest·조회 종류·호스트 자식 수거 여부만 넣는다. 원본 기기 응답은
명시적 `observation.data`로만 읽으며 repr·공개 결과·오류에 개인 기기 정보나 도구 출력을 넣지 않는다.
이 관찰의 `executionAuthority`는 `none`, `deviceCleanupConfirmed`는 `false`다.

이 단계는 보호 실행기의 입력·조회 구성 요소다. framework 의존성 전체, CoreDevice daemon,
선택 기기의 네트워크/backend 격리, 보호 서비스의 설치·재생·sanitation과
재시작 복구는 아직 연결하지 않았다. 기존 physical provider의 설치·XCTest 경로 전체가
이 클라이언트로 고정됐다고 해석하지 않는다. 실제 환경 qualification을 발급하지 않는다.

`D4_IOS_DEVICE_QUERIES`는 자체 Mach-O 프로토콜 대역으로 고정 argv·환경·응답·취소·정리
실패를 검사하고 기존 iPhone/프로필/자동 관찰 회귀를 실행한다. 실제 iPhone 수용 검사는 별도다.

## 원래 잠금을 상속하는 조회

`IOSDeviceGuardianTools(path, sha256)`로 지정한 Mach-O 감시기를
`IOSDeviceQueryDefinition(..., native_guardian=guardian)`에 고정할 수 있다. 감시기의 경로·해시도
조회 정의 digest에 포함하므로 다른 감시기로 바꿔 같은 작업의 소유권을 재사용할 수 없다.
`native/ios-device-guardian/main.c`는 배포 리소스에 포함되며 기존 macOS SDK의 clang으로 빌드한다.
이 정의의 `open_client(native_owner=owner)`는 원래 iOS 작업에서 발급한 살아 있는 owner를 요구한다.

조회 API에서 감시기는 원래 producer·기기 잠금을 직접 확인하고 고정 `device info details/apps/processes`만
실행한다. 별도 [설치 API](IOS-MOBILE-OPERATIONS.md)의 두 명령은 추가 dispatch handshake가 필요하다.
Python 부모의 생존 pipe가 닫히면 직접 자식을 종료·수거한 후 잠금을 놓는다.
감시기만 강제 종료되는 경우에도 도구 자식이 원래 잠금을 상속한다. 도구가 이 descriptor를
계속 유지하는지는 실제 도구·환경 수용에서 별도로 확인해야 한다.
감시기는 SDK의 별도 프로세스 그룹을 수거한 뒤 전용 completion pipe로 완료를 알린다.
감시기만 종료됐거나 pipe EOF만 보이면 수거 완료로 처리하지 않고 원래 작업을 보존한다.
SDK에는 completion pipe의 쓰기 descriptor를 상속하지 않는다.
자식의 일반 파일 크기에 256 KiB 제한을 적용하며 stdout/stderr 읽기·취소·기한 제한도 유지한다.
이는 CoreDevice daemon의 쓰기나 네트워크 접근을 제한하는 sandbox가 아니다.

정상 조회 결과에는 `nativeBindingDigest`를 추가한다. 기기 정리 여부는 계속 `false`이며,
호스트 프로세스 수거와 기기·daemon 정리를 구분한다. 원래 작업은 native 복구가 필요하므로
조회 종료만으로 파일 예약이나 기기 소유권을 해제하지 않는다.

실제 native 감시기와 자체 Mach-O 프로토콜 대역으로 부모 SIGKILL, 감시기 SIGKILL,
자식 종료까지의 잠금 유지, 취소, 도구 교체와 관리 명령 거절을 검사했다. 실제 CoreDevice나
개인 기기·pairing 파일은 이 검사에서 사용하지 않았다.
