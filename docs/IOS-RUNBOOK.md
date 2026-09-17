# iOS Simulator MVP

2026-09-09 구현·검증. 지원 확인 환경은 Xcode 27.0(27A5228h), iOS 27.0 arm64 Simulator다. iPhone 실기기·서명·TestFlight 앱·SwiftUI는 아직 검증하지 않았다.

## 확인한 결과

- Swift SDK의 실제 입력 기록 → iOS v2 번들 생성.
- 원본의 결함 3/3회 확인.
- `CounterLogic.swift`의 `return 2`만 `return 1`로 바꾼 별도 소스 복사본 빌드.
- 기존 `CounterLogicTests/testIncrementIsOne` 1개 통과, skip·실패 0.
- 원본에서 고정한 UI runner로 수정본 3/3회 정상 결과 확인.
- replace → tap(next) → navigate_back → scroll_to → tap(add)의 5개 이벤트 기록·재생 일치.
- Python 전체 테스트 46개 통과. Android v1 검증도 유지.

이 문서의 초기 결과는 `scripts/ios-sample-fix.json`의 오프라인 기준 패치다. 후속 실제 Claude 수정과 사례 확장은 [사례별 실행 문서](IOS-CASES.md)를 참고한다.

현재 워크스페이스의 결과:

- [수정 검증 HTML](../artifacts/ios-repair/report.html)
- [전체 job 증거](../artifacts/ios-repair/job.json)
- [적용한 패치](../artifacts/ios-repair/attempt-1/patch.diff)
- [입력 종류별 smoke 결과](../artifacts/ios-semantic-smoke/result.json)

실행 산출물은 `artifacts/`에 생성되며 새 체크아웃에는 포함되지 않을 수 있다. 아래 명령으로 다시 만든다.

## 준비

저장소 루트에서 실행한다. 이 작업에서 만든 전용 Simulator 이름과 UUID는 `artifacts/ios-session/simulator.json`에 남겼다. 다른 Simulator를 쓸 때는 부팅된 iOS Simulator UUID를 지정한다.

```bash
xcrun simctl list devices available
python3 -m reproloop ios-doctor --simulator <SIMULATOR_UUID>
```

꺼진 Simulator는 먼저 `xcrun simctl boot <SIMULATOR_UUID>`로 부팅하고 `xcrun simctl bootstatus <SIMULATOR_UUID> -b`로 준비를 확인한다. 기존 기기를 지우거나 초기화할 필요는 없다. 테스트는 전용 샘플 앱만 설치하고 앱의 메모리 fixture를 다시 만든다.

프로젝트는 `ios/project.yml`과 생성된 `ios/ReproLoop.xcodeproj`를 포함한다. 필요하면 `cd ios && xcodegen generate --spec project.yml`로 재생성한다. 외부 Swift package 의존성은 없으며 Simulator 빌드는 signing을 끈다. 전역 Xcode 선택이나 인증서·프로파일을 변경하지 않는다.

## 한 번에 실행

```bash
bash scripts/ios-demo.sh <SIMULATOR_UUID> artifacts/my-ios-demo
```

출력 디렉터리는 새 경로여야 한다. 원본과 모든 실패·성공 기록을 보존하며 기존 증거를 덮어쓰지 않는다.

## 단계별 실행

```bash
python3 -m reproloop ios-build \
  --simulator <SIMULATOR_UUID> --output artifacts/my-ios-build

python3 -m reproloop ios-record \
  --simulator <SIMULATOR_UUID> --build artifacts/my-ios-build \
  --output artifacts/my-ios-record

python3 -m reproloop ios-replay artifacts/my-ios-record/bundle \
  --simulator <SIMULATOR_UUID> --output artifacts/my-ios-baseline

python3 -m reproloop ios-repair artifacts/my-ios-record/bundle \
  --simulator <SIMULATOR_UUID> --patch-file scripts/ios-sample-fix.json \
  --output artifacts/my-ios-repair
```

`ios-record` 기본값은 XCUITest가 합성 QA 입력을 실행하는 방식이다. `--manual`을 추가하면 직접 앱에서 `QA` 입력, Add, Report를 수행한 뒤 터미널 Enter로 회수할 수 있다. SDK가 finalized metadata와 종료 경계를 모두 게시했는지, 이번 실행 이후의 새 기록인지 확인한다.

실제 AI 패치를 요청하려면 `--patch-file` 대신 `--agent claude`를 명시한다. 이때 허용된 Swift 소스와 합성 QA packet이 Claude로 전송된다. 도구 없는 JSON 교체안만 받으며, 현재는 사례별 지정된 제품 파일의 제한된 반환 표현식 변경만 허용한다. 범용 Swift 코드를 호스트에서 실행하지 않는다.

입력 종류별 검사:

```bash
python3 scripts/ios-smoke.py --simulator <SIMULATOR_UUID> \
  --build artifacts/my-ios-build --output artifacts/my-ios-smoke
```

## 증거와 보호 경계

번들 v2는 `platform=ios`, `executionEnvironment=simulator`, `ios-simulator-app` artifact를 명시한다. Android v1이나 실제 iPhone 산출물을 이 경로로 받아들이지 않는다.

원본 `.app`과 UI test products를 보존하고 각 run에서 사본으로 실행한다. 수정본 테스트도 원본의 UI runner를 사용하며, app 경로와 기대 build ID만 job 정책에 따라 바꾼다. `.xctestrun`은 Xcode가 생성한 v1 또는 v2 형식만 지원하며 다른 형식은 차단한다.

성공 판정에는 실제 XCTest 실행 1건, pass 1·skip 0·failure 0, `repro-result.json` 단일 attachment, 대상 Simulator ID·run ID·scenario digest·runtime build ID 일치가 모두 필요하다. UI에서 관찰한 count로 실제/기대 조건을 호스트가 다시 계산한다. 테스트 프로세스 종료 코드만으로 검증 완료를 선언하지 않는다.

증거 등급은 설치 영수증과 실행 build ID를 연결한 Simulator 증거다. Android의 설치 APK 직접 hash 확인이나 실기기의 강한 attestation과 동등하다고 표현하지 않는다.

원본 xcresult에는 Xcode가 자동 수집하는 부가 정보가 있을 수 있어 로컬에 보관한다. HTML과 AI에는 선택한 합성 필드만 전달한다. iOS SDK는 현재 UIKit 샘플에 소스로 통합되어 있으며 독립 Swift package 배포는 후속 범위다.

## 알려진 제한

- iPhone 실기기 어댑터·pairing·서명 검증은 후속 I3 단계다.
- SwiftUI, 일반 크래시 판정, 임의 앱의 입력 수집, 운영 데이터는 지원하지 않는다.
- 초기 fixture는 샘플의 메모리 상태다. Keychain·iCloud·서버 데이터 초기화의 일반 구현은 포함하지 않는다.
- 완료된 기록은 같은 앱 프로세스에서 자동으로 다시 시작하지 않는다. 새 QA 세션은 fixture부터 앱을 재시작한다.
- 개별 Xcode 명령에 timeout·출력 제한을 적용한다. 실패한 beta Xcode 작업은 로그 정리에서 지연될 수 있으며, 해당 실행은 성공으로 세지 않는다.
- 전체 앱 코드 수정 대신 정수 표현식 패치만 허용한다. 범용 패치는 별도의 빌드 격리와 서명 경계가 필요하다.
