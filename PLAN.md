# Repro Loop — 오픈소스 모바일 테스트 플랫폼 계획

2026-09-11 · 회사 QA 재현 루프와 실기기 공유 목표 반영

## 목표

사용자가 명확히 한 최종 목표는 **QA의 영상·실행 가능한 기록·시작 조건으로 회사 앱의 이슈를 재현하고, AI 수정 후 같은 기록으로 검증하며, 여러 Mac에 연결된 Android/iPhone을 공유하는 것**이다. [제품 기준·공개 사례·현재 간격·다음 검증](docs/QA-REPLAY-DEVICE-FARM.md)을 우선 기준으로 삼는다. 샘플 통과나 관찰 로그 생성만으로 전체 목표를 완료 처리하지 않는다.

STF를 그대로 사용하거나 포크에 의존하지 않고, 원격 수동 조작·자동화·기기 운영·기록·AI 재현/수정을 제공하는 self-hosted 모바일 테스트 플랫폼을 만든다. 사용자가 지정한 참고 범위는 Appium, BrowserStack App Live, AWS Device Farm, Corellium이다.

사람이 브라우저에서 기기를 직접 사용하고, 같은 세션을 AI나 CI에 넘길 수 있어야 한다. 세션·기기 제어·실시간 영상이 기반이며, 현재의 replay/repair 코드는 그 위에 연결한다.

## 기준 문서

공유 QA 기록·재현과 G9의 일반 수정 제안·보호 검증 조합을 구현했다. 현재 G9 소프트웨어 92개가 통과했고, 전체 고유 검사 1,026개 중 1,023개에 통과 근거가 있다. 고정 Android 의존성 검사 3개와 실제 VM·서명·모바일 격리·회사 앱·AI·두 Mac 수용 검사는 남아 있다. [수정 제안·검증 운영 문서](docs/PROJECT-REPAIR.md), [보호 실행 문서](docs/REPAIR-EXECUTION.md), [최신 인수인계](HANDOFF.md)에 구성·근거·남은 입력을 기록한다.

- [현재 구현 순서·목표별 검증 기준](docs/IMPLEMENTATION-DELIVERY-PLAN.md) — 이번 실행의 기준. 앞부분 보완 사항이 뒤의 계획 초안보다 우선한다.
- [플랫폼 아키텍처와 단계별 완료 기준](docs/PLATFORM-ARCHITECTURE.md)
- [공식 문서 비교와 설계 반영](docs/PLATFORM-REFERENCE-COMPARISON.md)
- [사람의 시연 기록·재생·자동화 생성](docs/DEMONSTRATION-REPLAY.md)
- [현재 구현·검증 현황](docs/IMPLEMENTATION.md)
- [기존 replay/repair 상세 계획](docs/REPLAY-PLAN.md)
- [iOS 어댑터 설계](docs/IOS-DESIGN.md)

기존 계획의 ‘ID 기반 샘플 우선’·‘좌표 fallback 제외’는 해당 검증 프로토타입의 범위다. 새 플랫폼의 원격 조작·화면 기반 AI를 금지하는 규칙으로 적용하지 않는다. 현재 동작 중인 샘플의 검증 기준은 구현 변경과 회귀 검사 없이 완화하지 않는다.

## 실행 순서

1. 서로 다른 두 Mac의 물리 Android/iPhone을 중앙 콘솔에서 안전하게 공유한다.
2. 실제 회사 QA 이슈의 영상·실행 기록·상태/서버 fixture를 묶고, 새 기기 배정에서도 반복 재현한다.
3. 원본 기록과 판정 기준을 보호한 상태로 AI 수정·회사 회귀 테스트·후보 반복 재현을 연결한다.
4. 검증한 흐름을 더 많은 앱·기기·사용자로 확장하고 미디어·장애 복구·운영을 고도화한다.

기존 세부 단계와 샘플 증거는 참고 자료로 유지한다. 실제 앱의 기술 스택과 이슈가 수용 기준을 결정한다.

물리 기기와 기존 Emulator/Simulator 운영은 공통 provider 모델로 다룬다. OS 가상화 엔진 자체와 고급 snapshot/분석은 별도 연구 트랙이며, Live 완료와 같은 성과로 표시하지 않는다.

## 현재 상태

현재 구현은 프로젝트 권한, 호스트 등록·독점 점유, fixture 준비, 불변 기록과 MP4, 원본 재현, 이슈 패키지, 워커 전송, 브라우저/CLI 수정 작업을 연결한다. 수정은 숫자 표현식에 한정하지 않으며, 원본·명세·보호 테스트·빌드/검증 규칙을 유지한다. 보호 검증 성공에는 측정된 빌드·서명, 후보 밖의 독립 검사, 동일 명세의 모든 후보 시도와 정리가 필요하다.

G9 검사 92개와 G0–G7·G8b 회귀 검사를 실행했다. 새로 통과한 고유 검사 998개와 변경되지 않은 G8a 25개를 합친 1,023개 통과 근거는 `artifacts/qa-delivery/g9-parent-covered-tests.json`에 있다. 고정 Android 의존성 검사 3개는 차단됐으며 전체 회귀 통과로 표시하지 않는다. 실제 브라우저에서 제안·보호 검증·패치 검토·390px 배치·권한 회수를 확인했다. 보호 실행의 VM·서명·기기는 명시적인 대역이므로 회사 QA 전체 수용 검사와 구분한다.

이 Mac에서 AVFoundation 및 Virtualization 지원은 실제 컴파일·실행으로 확인했다. 보호된 일반 후보 빌드를 실행할 게스트 이미지·오프라인 도구 모음은 아직 제공되지 않았으며, 지원 여부만으로 격리 환경 검증을 통과시키지 않는다. 실제 회사 앱·QA 시작 조건·두 번째 Mac의 검증은 대상 입력을 받은 뒤 수행한다.

### 이전 샘플·실기기 단계 기록

아래는 각 단계 당시의 검증 범위다. 현재 공통 서비스 상태는 위 문단과 인수인계를 따른다.

공통 세션 API·브라우저 콘솔·녹화/재생·JSON/Python 내보내기와 Android/iPhone 실기기 제어를 구현했다. Android는 continuous pointer·2-finger·약 14.4FPS를 검증했고, iPhone은 XCTest gesture-batch와 대기 중 약 3FPS sampled JPEG를 관측했다. [기기 Live 실행·검증](docs/DEVICE-LIVE.md)에 범위와 증거를 기록했다.

작업 큐·반복 실행·취소·이력 복원, 녹화 라이브러리와 엄격한 가져오기/복사, 세션 만료·재시작 격리, CLI·로컬 에이전트 도구를 제공한다. 같은 컴퓨터의 별도 worker 프로세스에서도 실제 Android 기기 재생과 반납을 확인했다. 외부 worker 운영이나 역할별 다중 사용자 권한까지 검증한 것은 아니다.

2026-09-10에는 iPhone 세 사례의 실제 Claude 수정·원본 3/3·수정본 3/3 및 보호된 로직 테스트를 실기기에서 검증했다. 브라우저 Live 카운터 녹화를 SDK 증거·로컬 화면 관찰·수정 작업·검증된 후보의 새 Live 세션으로 연결했다. [Live repair 실행·검증](docs/LIVE-REPAIR.md)에 결과를 정리했다.

Android 샘플의 같은 Live→실제 SDK→Claude→원본 3/3·회귀 검사·수정본 3/3→후보 재개도 실기기에서 완료했다.
이후 패키지·UI ID·fixture·빌드·수정 함수를 명시하는 앱 프로필을 구현하고, 이름을 바꾼 합성 앱으로 전용 Android 에뮬레이터에서 전체 흐름을 검증했다. [앱 프로필과 제한](docs/ANDROID-APP-PROFILES.md)에 계약과 근거를 기록했다.

Kotlin Views Activity의 지정한 클릭 핸들러에 로그를 자동 추가하는 명령도 제공한다. 기본은 [소스 수정 없는 빌드 계측](docs/BUILD-INSTRUMENTATION.md)이며, 컴파일된 클래스에 hook을 넣고 Release에는 런타임과 hook을 포함하지 않는다. [소스 삽입 방식](docs/AUTO-INSTRUMENTATION.md)은 선택 옵션으로 유지한다. 두 방식 모두 클릭 전후 숫자 상태를 SDK 이벤트와 소스 위치에 연결하며, SDK 호출과 Report 버튼이 없는 합성 앱으로 검증한다.

iOS도 [UIKit 자동 수집](docs/IOS-AUTO-INSTRUMENTATION.md)을 제공한다. 별도 복사본의 Debug 빌드에 런타임을 연결하고 기존 앱 함수·Info.plist를 보존한다. 앱의 로그 호출·Report 버튼 없이 입력과 버튼 실행 전후 상태를 기록한다. 고정 UIKit 프로필의 세 사례와 화면 왕복, Release 제외, 실행 정보 불일치와 background 전환 거절을 전용 Simulator에서 확인했다.

실제 대상 앱의 소스·fixture 연결과 전체 빌드 입력 확장, Compose/SwiftUI 자동 계측·수정, iPhone 연속 pointer/멀티터치, 고FPS 영상, WebDriver/Appium 전체 호환과 다중 사용자 운영은 후속 범위다. 현재 합성 앱의 구현·검증을 일반 앱 지원 또는 플랫폼 전체 완료로 표시하지 않는다.
