# 공식 문서 기반 플랫폼 비교와 설계 반영

확인일: 2026-09-09 · 범위: Appium, BrowserStack App Live, AWS Device Farm, Corellium 공식 문서

프로젝트 소스·QA 기록을 외부에 전송하지 않고 공개 문서만 조회했다. 아래의 ‘확인’은 문서가 설명하는 기능이며, ‘설계 결정’은 이를 참고해 우리가 선택한 구조다. 각 서비스의 비공개 서버 구현이나 내부 미디어 프로토콜을 역추정한 결과가 아니다.

## 1. 네 제품에서 참고할 축

| 참고 대상 | 공식 문서에서 확인한 핵심 | 우리 플랫폼의 설계 결정 |
|---|---|---|
| Appium | 플랫폼별 driver가 WebDriver 요청을 실제 자동화 기술에 매핑하며, iOS는 여러 계층을 거침 | 자체 기기 엔진과 외부 프로토콜 호환 계층 분리 |
| BrowserStack App Live | 실제 모바일 기기의 대화형 앱 테스트, 여러 앱 공급 경로·기기 비교·Local Testing | Live 콘솔, 설치 artifact 관리, 개발망 연결을 제품 기능으로 취급 |
| AWS Device Farm | 원격 수동 접근, 자동 테스트 실행 환경, 원격 세션의 Appium endpoint | 같은 기기 점유에 수동·자동화 controller 연결, CI host를 기기와 별도 관리 |
| Corellium | 가상 기기의 웹/API 입력, 저장 상태 및 RAM을 포함하는 snapshot | 가상 인스턴스 운영과 snapshot 종류를 명시하고, OS 가상화 엔진 개발을 독립 과제로 구분 |

근거: [Appium driver 구조](https://appium.io/docs/en/latest/intro/drivers/), [BrowserStack App Live](https://www.browserstack.com/docs/app-live), [AWS 원격 접근](https://docs.aws.amazon.com/devicefarm/latest/developerguide/remote-access.html), [Corellium 입력](https://support.corellium.com/getting-started/type-and-tap-on-virtual-devices), [Corellium snapshot](https://support.corellium.com/features/snapshots).

## 2. Live와 자동화를 같은 기기에 연결

**확인:** AWS는 원격 접근 세션의 응답에서 `remoteDriverEndpoint`를 제공하고, 로컬 Appium 코드가 그 endpoint에 연결하는 방법을 문서화한다. 이는 수동 세션과 자동화 연결을 같은 기기에 제공할 수 있다는 공개 사례다. [AWS Appium endpoint](https://docs.aws.amazon.com/devicefarm/latest/developerguide/appium-endpoint-interaction.html)

**설계 결정:** 하나의 LabSession이 기기를 소유하고, 그 안에 사람·AI·WebDriver controller가 연결된다. 여러 사람이 영상을 관찰할 수 있어도 활성 입력 controller는 하나다.

두 ID의 수명은 분리한다.

- LabSession: 기기 배정, 설치 상태, 영상·기록과 종료 후 정리를 소유.
- ControllerSession: browser/manual, AI, WebDriver 등 특정 입력 주체의 권한을 소유.

기존 Live 세션에 붙은 WebDriver client의 quit은 자동화 조작권을 해제한다. 자동화가 단독으로 만든 세션은 client 종료 시 기기까지 반납하는 정책을 기본으로 둔다. 인계 중에는 앱을 자동 초기화하지 않는다. 재현을 위해 초기화가 필요한 경우는 별도 replay 작업으로 구분한다. 이 수명 규칙은 우리의 제안이며 AWS의 세부 종료 동작을 설명한 것이 아니다.

## 3. 호환 범위를 작게 정의

**확인:** Appium의 문서는 WebDriver 명령을 driver에 위임하는 구조와 actions/release actions 같은 endpoint를 설명한다. 플랫폼에서 실제 동작을 구현하는 책임과 프로토콜 표면이 분리된다. [Driver 구조](https://appium.io/docs/en/latest/intro/drivers/), [WebDriver reference](https://appium.io/docs/en/latest/reference/api/webdriver/)

**설계 결정:** STF나 Appium 서버를 필수 백엔드로 두지 않고, 자체 Session/Input API 앞에 호환 어댑터를 구현한다. 아래는 앞으로 만들 호환 단계이며 현재 구현된 endpoint 목록이 아니다.

| 단계 | 대상 | 완료 기준 |
|---|---|---|
| C0 | session 생성/종료, status, timeout, screenshot, actions/release | 선정한 client가 연결되고 입력 해제·timeout·잘못된 session 오류가 일관됨 |
| C1 | 요소 조회·텍스트·기본 앱 lifecycle·방향 | 지원 selector와 플랫폼 의미를 명시하고 실제 동작 검사 |
| C2 | 검증된 모바일 확장 명령·기존 테스트 suite | 호환표와 회귀 검사를 갖춘 명령만 확대 |

참조 endpoint 중 `POST /session/:sessionId/actions`와 `DELETE /session/:sessionId/actions`는 입력 상태의 생성과 해제를 분리한다. 우리 controller 교체·연결 종료에서도 눌린 pointer/key를 해제하는 계약으로 연결한다. 미지원 명령을 성공으로 돌려주거나 다른 동작으로 조용히 바꾸지 않는다.

프로토콜 endpoint 몇 개가 동작하는 것만으로 모든 Appium driver/client와 호환된다고 표시하지 않는다. Appium/W3C와 관련된 이름은 호환성 설명에 사용하고 특정 제품의 내부 코드나 동작을 그대로 복제한다고 약속하지 않는다.

## 4. 기기뿐 아니라 테스트 host도 자원

**확인:** AWS custom test spec은 준비·실행·후처리 단계를 구분한다. AWS의 test host 문서는 실행 host를 기기와 별도로 설명하고, 실행마다 host를 정리해 재사용하지 않는 운영 방식을 명시한다. [Test spec](https://docs.aws.amazon.com/devicefarm/latest/developerguide/custom-test-environment-test-spec.html), [Test hosts](https://docs.aws.amazon.com/devicefarm/latest/developerguide/custom-test-environments-hosts.html)

**설계 결정:** 우리도 Device Provider와 Test Worker를 분리한다. 특히 iOS 작업은 기기만 배정해서 끝나지 않고 Xcode·빌드·서명 가능 상태를 함께 확인해야 한다.

첫 CI 계약은 prepare → run → collect → cleanup으로 둔다. 사용자 test package 실행은 worker의 격리된 작업 범위에서 수행하고, 기기 관리 권한이나 host 전체 파일 접근을 그대로 제공하지 않는다. 실패해도 collect/cleanup 상태를 남긴다. 기기 reset과 host 작업 디렉터리 정리는 다른 작업이다.

초기 self-hosted 배포는 단순하게 시작하되, 여러 사용자의 임의 테스트를 받기 전에 worker 격리와 권한 경계를 갖춘다. 현재 샘플의 반환 표현식 제한을 임의 CI 실행의 격리로 취급하지 않는다.

## 5. Live 기능에는 앱 네트워크도 포함

**확인:** BrowserStack App Live는 실기기 앱 상호작용과 Local Testing을 통한 내부 개발·staging 서버 접근을 설명한다. 문서의 Live와 App Live를 혼동하지 않고, 여기서는 앱 테스트용 App Live를 참고했다. [App Live 문서](https://www.browserstack.com/docs/app-live), [App Live 소개](https://www.browserstack.com/docs/app-live/overview/introduction)

**설계 결정:** 제어 API와 영상 전송 외에, 테스트 앱이 개발 서버에 도달하는 Network Connector를 별도 경계로 둔다. 브라우저에서 화면이 보인다는 사실만으로 앱의 API가 개발자 PC나 사내망에 연결된 것은 아니다.

초기에는 네트워크 연결 없이도 되는 앱으로 Live 입력을 검증한다. 이후 세션에 묶인 사설 endpoint 연결을 추가한다. 연결 라우팅, 네트워크 지연/손실 제어, 트래픽 내용 수집은 다른 capability다. 앱 TLS나 기기의 신뢰 설정을 조용히 변경하는 기능을 기본값으로 두지 않는다.

## 6. 가상 기기의 범위를 정확히 나누기

**확인:** Corellium은 웹의 mouse/keyboard 입력과 API 기반 터치·swipe·문자 입력을 설명한다. [입력 문서](https://support.corellium.com/getting-started/type-and-tap-on-virtual-devices)

**확인:** snapshot 문서는 storage만 저장하는 종류와 RAM을 포함하는 live snapshot을 구분한다. live snapshot에서 새 기기를 만들 때 RAM 상태는 복제되지 않는다는 제한도 명시한다. [Snapshot 문서](https://support.corellium.com/features/snapshots)

**확인:** CHARM SDK 문서는 hypervisor와 custom device model을 다루는 개발 영역을 설명한다. 이를 원격 화면 서버만의 기능으로 축소하면 안 된다. [CHARM SDK](https://support.corellium.com/environments/charm-sdk)

**설계 결정:** ‘snapshot 지원’이라는 단일 boolean 대신 아래를 명시한다.

| 종류 | 우리 API에 기록할 의미 |
|---|---|
| app-fixture | 지원하는 앱 데이터/서버 seed를 다시 준비 |
| storage snapshot | 저장 상태를 복원하고 다시 부팅 |
| live snapshot | provider가 지원하는 메모리+저장 상태 복원 |
| clone | 새 인스턴스 생성; 메모리 이어받기 여부 별도 표기 |

각 snapshot에는 provider, 원본 artifact·OS 식별자, 복원 방식과 호환 조건을 기록한다. 외부 서버·로그인·시간·외부 장치 상태까지 복원된다고 가정하지 않는다.

첫 제품은 물리 기기와 기존 Emulator/Simulator를 관리하는 provider부터 구현한다. 자체 OS 가상화·가상 하드웨어 모델·깊은 상태 분석은 별도 연구 트랙으로 유지한다. 이는 Corellium과 같은 가상화 계층까지 장기적으로 검토하되, 초기 Live 완료와 혼동하지 않기 위한 범위 결정이다.

## 7. 우선순위와 첫 플랫폼 릴리스

다음은 조사 결과를 반영한 우리의 제안이다.

1. **P0:** LabSession/ControllerSession, 기기 capability, 입력/영상 타임라인과 worker 계약.
2. **P1:** 독자적인 Live 콘솔과 한 provider에서의 실제 연속 입력. 기기 없이 가능한 부분은 mock provider와 Simulator로 진행하되 실제 provider 성능을 따로 검증.
3. **P2:** 작은 팜의 기기 등록·배정·권한·종료 정리·복구. worker와 앱 네트워크 연결을 별도 관리.
4. **P3~P4:** 추가 플랫폼 Live 및 명시적인 C0 자동화 호환 범위. Live에 붙은 자동화와 독립 CI 작업을 모두 수용.
5. **P5~P6:** 화면 기반 AI, 사람/AI 인계, 현재 replay/repair 엔진 연결.
6. **R:** 고급 snapshot·가상화 엔진·분석의 독립 검증.

첫 릴리스의 완료 기준은 ‘내부에 연결된 기기를 웹에서 골라 실제로 조작하고, 같은 세션의 조작권을 자동화에 넘기며, 기록·결과를 회수하고 안전하게 반납한다’다. 외부 상용 제품과 전체 기능 동등성을 출시 기준으로 묶지 않는다.

## 8. 확인하지 않은 것

각 서비스의 비공개 미디어 구현, 특정 저수준 입력 기술, 정확한 latency, 비용, 전체 device/OS 조합, 라이선스 적합성 판단은 이 비교에서 확인하지 않았다. STF의 유지보수 상태와 포크 선호도도 이번 조회 범위에 포함하지 않았다. 제품 문서는 기능·공개 API의 근거이며, 독자 구현의 법적 판단이나 구현 난이도 보증이 아니다.
