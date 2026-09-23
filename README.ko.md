# Repro Loop

[English](README.md)

Repro Loop는 STF에 의존하지 않는 self-hosted 모바일 QA 플랫폼입니다. 회사 QA
이슈를 영상·행동·시작 조건으로 기록하고, Android와 iOS에서 결정적으로
재현하며, AI가 생성한 수정을 적용하고, 같은 승인 원본으로 수정 후보를 다시
검증합니다.

wheel 설치와 개발 폴더 밖에서의 로컬 실행은 [설치 가이드](docs/INSTALLATION.md)를
따릅니다. 일반 앱 제품 경로의 진행 상태는
[현재 실행 계획](docs/PRODUCT-DELIVERY-PLAN.md)에 있습니다(계획 문서는 r51
기준이며 실기기 진행분은 [HANDOFF.md](HANDOFF.md)를 참고하세요).

## 현재 상태

검증된 것과 아직 남은 것을 구분해 둡니다.

- 샘플 경로(기록·재현·수정·재검증)는 Android 기기, iOS Simulator, iPhone
  실기기에서 끝까지 동작합니다. 카운터 결함(`Add` 한 번에 카운트가 2 증가)에서
  원본 3/3 재현·수정본 3/3 통과를 확인했고, 이 결과에는 실제 Claude가 생성한
  패치도 포함됩니다.
- 공유 QA 경로(기록·영상·이슈 패키지·승인된 재현 명세)는 같은 Mac에서 실제
  워커 프로세스 두 개와 합성 앱, 실제 MP4·브라우저로 검증했습니다
  ([가이드](docs/ISSUE-WORKFLOW.md)).
- 선언된 제품 파일의 보호 수정은 구현돼 있습니다([G9 경로](docs/PROJECT-REPAIR.md)).
  정식 G9 소프트웨어 검사 92개가 통과했습니다.
- iPhone 실기기에서 보호 라이프사이클 전체가 완주했습니다. 기기
  qualification·서비스 조합·이슈 기록·명세 승인·3/3 재현·AI 수정
  `verified`까지 — 독립 observer 프로세스가 fixture로 준비된 기기 상태를
  대조해 확인합니다.
- 실제 제품 앱(번들 샘플이 아닌 앱)에서 같은 라이프사이클을 완주했습니다.
  수정 후보는 verified됐고, 결함을 고치면서 네트워크 egress를 일으킨
  후보는 `egress_violation`으로 거절됐습니다 — 기기 바이트 카운터와
  패킷급 캡처 증거로, Wi-Fi·cellular-only 양쪽에서 fail-closed를
  확인했습니다.
- 아직 남은 것: 격리 VM 빌드 경로(검증된 것은 호스트에서 직접 빌드하는
  host-build 경로이며 격리로 표시하지 않습니다), 두 대의 Mac을 쓰는
  구성의 수용.

## 제품 방향

제품이 지향하는 것은 원격 수동 조작·자동화·팜 운영을 공통 세션 위에 구성하고
기록·AI 재현/수정을 연결하는 self-hosted 모바일 테스트 플랫폼입니다.
[플랫폼 방향과 설계](docs/PLATFORM-ARCHITECTURE.md),
[공식 문서 비교](docs/PLATFORM-REFERENCE-COMPARISON.md),
[기록·재생 기능](docs/DEMONSTRATION-REPLAY.md)을 참고하세요.

일반 UIKit과 Android Views 앱의 자동 관찰은 공개 빌드 입력과 UI ID를 선언해
준비합니다([UIKit](docs/IOS-APP-OBSERVATIONS.md),
[Android Views](docs/ANDROID-APP-OBSERVATIONS.md)). 관찰 프로필은 원본을
보존하고 fixture·수정 정책과 분리되며, 공유 서비스의 일반 앱 세션에
연결됩니다.

## 샘플 명령의 지원 대상

아래 명령은 포함된 Android 샘플 앱과 iOS Simulator 샘플 앱만 대상으로
합니다. `Add`를 한 번 누르면 카운트가 2 증가하는 결함을 기록하고, buggy
빌드 변형은 그대로 둔 채 비즈니스 로직만 고친 후보로 다시 검증합니다.
Python 호스트는 외부 패키지 없이 실행됩니다.

## iOS Simulator

Swift 기록 SDK·UIKit 샘플·XCUITest batch runner가 하나의 파이프라인으로
연결돼 있습니다. 카운터·중복 제출·초기화 실패의 세 사례에서 실제 Claude
패치가 기존 회귀 테스트를 통과했고, Simulator와 iPhone 실기기 모두에서
원본 3/3 재현·수정본 3/3 정상 결과를 확인했습니다. 카운터 사례는
[Live 녹화에서 AI 수정까지](docs/LIVE-REPAIR.md) 연결돼 있습니다.

```bash
bash scripts/ios-demo.sh <BOOTED_SIMULATOR_UUID> artifacts/my-ios-demo
```

[iOS 실행 방법과 검증 결과](docs/IOS-RUNBOOK.md),
[실제 AI 수정·세 가지 버그 사례](docs/IOS-CASES.md),
[실패 경로 QA](docs/QA-REPORT.md)를 참고하세요. 기존 Android 번들 형식(v1)과
iOS 번들 형식(v2)은 별도로 검증합니다.

## Android 빠른 시작

필요한 환경은 Python 3.11+, JDK 17, Android SDK 35·build-tools·
platform-tools, Gradle 8.14.5(wrapper 포함), USB 디버깅이 허용된 Android
기기(API 26+)입니다. 아래 빌드는 오프라인 캐시가 준비된 환경을 기준으로
하며, 처음 쓰는 환경은 Android 플러그인·Kotlin·JUnit 의존성 준비가
필요합니다.

```bash
cd <repro-loop 클론 경로>
python3 -m reproloop doctor
python3 -m reproloop build --receipt artifacts/build.json
```

`build`는 샘플 앱과 ID 기반 입력 드라이버를 함께 빌드하고 소스·APK 해시를
영수증으로 저장합니다. 기본 도구 경로는 macOS 기본 설치 경로에서 자동으로
찾으며, 다른 환경은 `--gradle`, `--java-home`, `--sdk-home`을 지정하거나
`JAVA_HOME`, `ANDROID_HOME`을 설정할 수 있습니다.

`--scripted`로 합성 QA 동작을 실행하면 첫 번들이 자동으로 생성됩니다.

```bash
python3 -m reproloop record \
  --apk android/sample/build/outputs/apk/buggy/debug/sample-buggy-debug.apk \
  --driver-apk android/driver/build/outputs/apk/debug/driver-debug.apk \
  --receipt artifacts/build.json \
  --scripted --output artifacts/qa-bundle

python3 -m reproloop validate artifacts/qa-bundle
python3 -m reproloop replay artifacts/qa-bundle --output artifacts/original-runs
```

직접 QA를 하려면 `--scripted`를 빼고 실행합니다. 샘플 앱에서 `QA`를 입력하고
`Add`를 한 번 누른 뒤 터미널에서 Enter를 누르면 `Report` 동작으로 세션이
freeze(고정)됩니다. 기록 전용 버튼은 재생 이벤트에 들어가지 않습니다. 여러
기기가 연결되어 있으면 `--serial`로 선택합니다.

전용 샘플 패키지 `io.reproloop.sample`은 각 실행 전에 초기화됩니다. 기기
화면을 켜고 잠금 해제한 상태에서 실행합니다.

## 수정 루프

네트워크 없이 준비된 기준 패치로 전체 파이프라인을 실행합니다. 이 실행은
결과에 AI 실행으로 기록되지 않습니다.

```bash
python3 -m reproloop repair artifacts/qa-bundle \
  --patch-file scripts/sample-fix.json \
  --output artifacts/offline-repair
```

Claude CLI가 로그인된 환경에서는 샘플 소스와 합성 QA 기록을 보내 실제
패치를 생성할 수 있습니다.

```bash
python3 -m reproloop repair artifacts/qa-bundle \
  --agent claude --output artifacts/claude-repair
```

원본 3회에서 같은 결함을 확인한 다음 별도 작업 디렉터리에 패치를
적용합니다. 설치된 수정 APK의 해시가 빌드 결과와 일치하고, 보호된 JUnit
회귀 테스트가 실제로 통과하고, 수정본 3회가 정상이어야 `verified`가 됩니다.
결과가 섞이면 `inconclusive`이며 성공한 실행만 골라내지 않습니다.

수정 허용 범위는 `CounterLogic.kt`의 정수 증가 표현식입니다. 에이전트가
다른 코드·계측·fixture·테스트·빌드 설정을 바꾸면 빌드 전에 차단합니다.
일반 프로젝트의 선언된 제품 파일 수정은 별도의
[G9 경로](docs/PROJECT-REPAIR.md)를 사용합니다.

한 번에 오프라인 예제를 실행하려면 `bash scripts/demo.sh artifacts/my-demo`를
사용합니다. 출력 디렉터리는 새 경로를 지정해야 하며 기존 증거를 덮어쓰지
않습니다.

## 결과 보기

지정한 출력 디렉터리의 `report.html`과 `job.json`/`result.json`을
확인합니다. 수정 시도마다 소스 복사본, `edits.json`, `patch.diff`, 빌드
영수증, 반복 실행 증거가 남습니다. 원래 샘플 소스는 기준선으로 보존됩니다.

반환 코드는 성공한 재현·수정이면 0, 조건 미충족·환경 차단이면 2, CLI 입력
취소이면 130입니다.

## 테스트

```bash
python3 -m unittest discover -s tests -t . -v

# 정상(fixed) 빌드의 회귀 테스트
cd android
./gradlew --offline :sample:testFixedDebugUnitTest :sample:assembleFixedDebug :driver:assembleDebug
```

원본의 `:sample:testBuggyDebugUnitTest`는 결함 때문에 의도적으로
실패합니다. 수정 루프가 CounterLogic을 패치한 복사본에서는 같은 테스트가
통과해야 합니다. `NO-SOURCE`나 건너뛴 테스트를 검증 성공으로 인정하지
않습니다.

실기기에서 입력·스크롤·화면 이동·뒤로 가기와 민감 입력 거부를 확인하는 추가
스크립트는 `scripts/device_smoke.py --help`를 참고하세요.

## 구성과 범위

- `android/sdk`: opt-in 기록 SDK — 순서가 있는 JSONL, 세션 freeze, 불완전
  기록 표시
- `android/sample`: 버그·정상 빌드와 보호된 회귀 테스트
- `android/driver`: UiAutomation의 resource ID로 관찰·탭·입력·스크롤·뒤로
  가기
- `reproloop`: 번들 검증, 컴파일, ADB 실행, 반복 판정, 수정 오케스트레이터,
  HTML 리포트
- `ios`: Swift Recorder·UIKit 샘플·보호된 XCUITest와 logic regression
- `reproloop/ios_*`: Simulator 빌드·v2 번들·batch 실행·Swift 수정 검증
- `schemas`: 기록·판정 데이터의 공개 형식
- `tests`: 실패·변조·반복 결과·패치 경계 검증

[iOS 지원 설계](docs/IOS-DESIGN.md),
[실행 계약과 현재 제한](docs/CONTRACTS.md),
[구현·검증 현황](docs/IMPLEMENTATION.md), [전체 계획](PLAN.md)을 함께
확인하세요. 앱별 관찰·fixture·독립 검증은 iPhone 실기기의 실제 제품 앱에서
검증됐습니다. 두 대의 Mac 수용과 격리 VM 빌드 경로가 남아 있습니다.
지원 범위는 샘플 경로와 공유 QA 경로별로 확인하세요.

## Live 콘솔

`python3 -m reproloop live-serve --demo`로 로컬 콘솔을 실행합니다. 실제 iOS
Simulator 연결, 녹화/재생과 Python 스크립트보내기는
[Live 실행 문서](docs/LIVE-RUNBOOK.md), 검증 결과는 [Live QA](docs/LIVE-QA.md)를
참고하세요.

기기 연결 없이 작업 큐와 녹화 라이브러리를 확인하려면
`python3 -m reproloop live-serve --demo --demo-count 2`를 실행하세요.
운영·CLI·에이전트 도구는 [운영 문서](docs/LIVE-OPERATIONS.md)를
참고하세요.

실제 Android의 연속 터치·스트리밍·worker 실행과 iPhone 실기기 서명·설치·
Live 녹화 재생 결과, 검증 한계는 [기기 Live 문서](docs/DEVICE-LIVE.md)를
참고하세요.

여러 사용자와 등록된 호스트를 사용하는 중앙 coordinator는 로컬 콘솔과
별도 모드입니다. 역할·프로젝트 권한, 일회용 host 등록(enrollment), TLS/CSRF,
비밀값을 포함하지 않는 설정 파일, 관리자 CLI는
[공유 coordinator 운영 문서](docs/SHARED-COORDINATOR.md)를 참고하세요.
