# 구현 현황 — 2026-09-12

현재 공유 QA 기록·fixture 준비·불변 MP4/이슈 패키지·원본 재현·워커 전송과 일반 수정 제안·보호 검증 조합을 구현했다. 시작 문서는 [공유 QA 화면](ISSUE-WORKFLOW.md), [워커 운영](WORKER-RUNTIME.md), [프로젝트 수정·검증](PROJECT-REPAIR.md)이다.

G9 필수 92개가 통과했고 현재 고유 검사 1,026개 중 1,023개에 통과 근거가 있다. 고정 Android 의존성 3개와 실제 회사 앱·AI·VM·서명·기기 격리·두 Mac 수용 검사는 남아 있다. [최신 인수인계](../HANDOFF.md)와 `artifacts/qa-delivery/g9-parent-covered-tests.json`이 현재 상태다. 아래는 이전 샘플 단계부터의 이력이며 당시의 제한과 테스트 수를 현재 상태로 읽지 않는다.

## 초기 구현 — 2026-09-09

현재 버전은 Android 샘플과 iOS Simulator용 샘플을 지원하는 MVP입니다. iOS에서는 카운터·중복 제출·초기화 실패의 세 가지 독립 사례를 실제 Claude 수정까지 검증했습니다. 전체 계획의 모든 플랫폼·수용 사례를 완료한 것은 아닙니다.

## 구현된 경로

1. fixture부터 SDK 이벤트 기록과 Report freeze.
2. 원본 APK·소스 영수증·기록·oracle의 무결성 확인.
3. 기록 이벤트를 ID 기반 시나리오로 결정적으로 컴파일.
4. 기기 독점·원본 재설치·앱 초기화·원본 반복 재현.
5. prompt-only Claude 또는 오프라인 patch-file 어댑터.
6. 격리된 소스 복사본에서 제한된 CounterLogic 수정.
7. buggy flavor 재빌드·실제 JUnit 실행·설치 해시 확인·수정본 반복 검증.
8. 결과 JSON, 이스케이프된 HTML, 패치와 실행별 증거 저장.

## 확인한 검증

- Python 단위·계약·오케스트레이터 경계 테스트 60개 통과(Android v1 + iOS v2, 사례 확장·실패 경로 QA 포함).
- Gradle 오프라인 Android 샘플·드라이버 빌드 및 sample·driver·sdk Android lint 통과.
- 원본 카운터의 JUnit 테스트는 의도대로 실패하고 정상 flavor 테스트는 통과.
- 별도 소스 복사본에 기준 패치를 적용한 뒤 buggy flavor 빌드와 기존 회귀 테스트 1개 통과, 보호 경로 유지 확인 (`artifacts/offline-build-check/result.json`).
- 연결된 실제 Android 기기에 드라이버·샘플 설치 및 설치 APK SHA-256 일치 확인.
- 실제 드라이버 입력이 SDK의 replace/tap 이벤트 2개로 기록되고 번들로 검증됨.
- 이미 포커스가 있는 EditText에 재차 포커스를 요구해 재생이 실패하는 문제를 발견하고 수정.

마지막 실행 시점에는 USB 기기가 연결 목록에서 사라져, 최종 소스 기준의 실기기 원본 3회·패치 후 3회 통합 검증과 확장 smoke 검사가 대기 상태다. 이전 실패 실행은 `artifacts/repair-counter`에 보존한다. Android 실기기에서의 실제 AI 수정 검증은 남아 있으며, iOS Simulator의 실제 Claude 검증은 아래에 별도로 기록했다.

## 남은 실행 검증

기기를 다시 연결하고 다음 명령으로 최종 버전을 확인한다. 출력은 기존 증거와 다른 새 경로를 사용한다.

```bash
bash scripts/demo.sh artifacts/verified-demo

python3 scripts/device_smoke.py \
  --apk android/sample/build/outputs/apk/buggy/debug/sample-buggy-debug.apk \
  --driver-apk android/driver/build/outputs/apk/debug/driver-debug.apk \
  --receipt artifacts/verified-demo/build.json \
  --output artifacts/verified-smoke
```

소스·receipt를 바꾸면 원본 APK와 현재 소스의 연결을 다시 확인해야 한다. receipt가 없는 임의 APK를 AI 수정 성공으로 처리하지 않는다.

Python wheel 패키징은 로컬 setuptools 빌드 의존성이 없어 확인하지 못했다. 소스에서 `python3 -m reproloop`로 실행하는 경로는 확인했으며 런타임 외부 패키지는 필요 없다.

## iOS Simulator 구현

[iOS 실행 방법과 증거](IOS-RUNBOOK.md)를 추가했다. 실제 SDK 기록, Simulator 원본 3/3 결함 재현, 제한된 Swift 패치, 기존 logic test 1개 통과, 원본의 고정된 UI runner를 통한 수정본 3/3 정상 결과를 확인했다. 스크롤·화면 이동·뒤로 가기를 포함한 5개 이벤트의 기록·재생도 통과했다.

세 가지 독립 사례에서 실제 Claude 수정과 Simulator·iPhone 실기기 검증을 완료했다. Android·iPhone Live 입력·녹화 재생은 DEVICE-LIVE.md, Live 카운터 녹화→실제 Claude 수정→검증된 후보 앱 재개는 LIVE-REPAIR.md에 증거를 기록했다. SwiftUI와 일반 앱 지원은 아직 검증하지 않았다.

## 실제 AI·실패 경로 QA·사례 확장

[종합 결과](../artifacts/ios-ai-cases/index.html)와 [검증된 JSON 집계](../artifacts/ios-ai-cases/validated-summary.json)에 실제 실행 증거를 모았다.

| 사례 | 실제 Claude 수정 | 원본 재현 | 회귀 검사 | 수정본 정상 |
|---|---|---|---|---|
| 카운터 | return 2 → return 1 | 3/3 | 1/1 | 3/3 |
| 중복 제출 | return true → return !submitted | 3/3 | 1/1 | 3/3 |
| 초기화 실패 | return previous → return 0 | 3/3 | 1/1 | 3/3 |

모두 첫 번째 패치 제안으로 통과했다. 각 사례는 별도 소스 복사본에서 해당 제품 파일 하나만 변경했고, 다른 제품·fixture·UI runner·회귀 테스트는 유지했다. 현재 소스와 원본 policy, 생성된 패치, 원본 runner, 18회 UI 실행 증거를 대조했다.

[실패 경로 QA](QA-REPORT.md)에서는 실제 문제 5개를 수정했다. 마지막 실행 후 소스/runner 변조, baseline 취소 저장, SIGTERM을 무시하는 자식 프로세스 정리가 포함된다. 전체 테스트는 60개가 통과했다.

## 플랫폼 방향 전환

[플랫폼 아키텍처](PLATFORM-ARCHITECTURE.md)를 상위 제품 계획으로 추가했다. 현재 코드는 replay/repair 기반 프로토타입으로 보존하며, 다음 구현 우선순위는 공통 세션·실시간 화면·원격 수동 조작·팜 운영이다. STF 코드/포크에 종속하지 않는 방향이다. 이 방향 전환 자체가 Live 플랫폼 구현 완료를 의미하지 않는다.

## 공식 문서 비교 반영

사용자 승인 후 Appium·BrowserStack App Live·AWS Device Farm·Corellium의 공식 문서를 조회했다. [비교와 설계 결정](PLATFORM-REFERENCE-COMPARISON.md)에 근거를 남겼으며, LabSession/ControllerSession 분리, C0~C2 호환 범위, test host와 Network Connector, snapshot 복원 의미를 상위 설계에 반영했다. 제품 내부 구현을 확인하거나 Live 기능을 구현한 것은 아니다.


## Live 세션 구현

공통 세션·독점 점유·controller epoch·입력 영수증·불변 녹화를 구현했다. 브라우저에서 실제 iOS Simulator를 지속 XCUITest로 조작하고 녹화/재생한다. JSON 및 Python replay 스크립트 내보내기와 같은 세션의 자동화 조작권 인계를 추가했다. Android ADB batch baseline은 실제 기기 검증 대기 상태다.

[Live 실행 방법](LIVE-RUNBOOK.md), [77개 테스트와 실제 Simulator 검증](LIVE-QA.md)을 참조한다. 현재 영상은 sampled frames, 입력은 pointer release 시 gesture-batch다. 전체 플랫폼 완료를 의미하지 않는다.


## Live 운영 코드 확장

단일 host의 기기별 FIFO 자동화 큐, 병렬 시작, 반복·취소·timeout·정리 영수증과 서버 재시작 후 이력 복원을 구현했다. 녹화 가져오기/속도 복사/라이브러리, 세션 heartbeat·만료·미정리 기기 격리, CLI와 JSON-lines 에이전트 도구를 연결했다. 전체 Python 테스트는 128개이며 이번 확장은 합성 기기·로컬 HTTP·브라우저로 검증했다. [운영 문서와 증거](LIVE-OPERATIONS.md)를 참조한다.


## 실제 Android Live와 worker

연속 pointer·멀티터치·binary frame stream을 추가하고 SM-S947N(API 36)에서 원본 재생 3/3, drag, 2-pointer, inactivity 복구, 회전 cancel, ADB forward 재연결 cleanup을 확인했다. 별도 worker 프로세스의 재생 결과 count 2를 기존 ID driver로 확인했다. [기기 검증 문서](DEVICE-LIVE.md)에 실행 APK와 최신 소스 재빌드 제한을 구분했다.
