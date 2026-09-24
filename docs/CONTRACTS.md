# 실행 계약 v0.1

이 문서는 기존 샘플의 v0.1 계약이다. 새 명시적 앱 프로필·bundle v2 계약과 지원 한계는 [Android 앱 프로필](ANDROID-APP-PROFILES.md)에 정리했다.

이 구현의 실행 대상은 `io.reproof.sample`이다. 호스트는 이 패키지와 `io.reproof.driver`만 설치·초기화한다. Python 표준 라이브러리만 사용하며 Android는 Kotlin·플랫폼 Views·UiAutomation으로 구현한다.

## 기록과 시작 상태

`record`는 APK 설치를 확인하고 앱 데이터를 초기화한 뒤 fixture `default@1`로 시작한다. SDK는 이후 이벤트 순서와 Report 시점의 종료 경계를 보존한다. 허용 입력은 빈 문자열·QA·Test다. 민감 입력은 저장하지 않으며 그 세션은 유효한 재생으로 인정하지 않는다.

원본 APK와 `capture.json`, `oracle.json`의 SHA-256을 manifest에 묶는다. 예상 목록 외의 파일, 링크, 손상, 중복 JSON key와 잘림·유실 기록은 컴파일을 차단한다. 기록 자체에 명령이나 파일 경로를 넣어 실행시킬 수 없다.

시나리오는 규칙 기반으로 생성한다. 각 사용자 이벤트에 `sourceEventIds`를 붙이고 입력 순서·동작·파라미터를 보존한다. UI 좌표를 대신 누르는 fallback은 없다. ID가 없거나 모호하면 실행이 실패한다.

## 반복과 증거

원본은 같은 결함을 3회 관찰해야 `reproduced`다. 수정본은 3회 정상 결과, 설치·소스·빌드 증거, 보호된 회귀 테스트의 실제 실행까지 확인해야 `verified`다. 결과가 섞이거나 원본과 정상 조건 모두에 해당하지 않으면 `inconclusive`다. 성공한 실행만 골라서 횟수를 채우지 않는다.

각 실행은 데이터 초기화·fixture 준비·설치된 APK의 SHA-256 확인을 반복한다. 기기는 파일 잠금으로 단일 호스트 프로세스가 독점한다. 프로세스가 종료되면 OS가 잠금을 회수한다. 다른 호스트나 사람이 수행한 조작의 완전한 탐지, 원격 팜 분산 lease는 아직 지원하지 않는다.

`run-*.json`, `result.json`/`job.json`, 정적 `report.html`이 증거다. HTML은 사용자 내용을 이스케이프하고 스크립트를 허용하지 않는다. 기기 serial은 해시로 바꾼다. 스크린샷·전체 logcat·네트워크 본문은 이 버전에서 수집하지 않는다.

## 패치와 독립 검증

`repair`는 원본 APK의 빌드 영수증과 현재 소스 digest가 같아야 시작한다. Android 소스를 별도 디렉터리로 복사하고, 에이전트에게는 `CounterLogic.kt`와 합성 QA 시나리오만 전달한다. 에이전트는 파일을 직접 수정하지 않고 JSON 텍스트 교체안을 반환한다.

수정 허용 파일은 `sample/src/main/java/io/reproof/sample/CounterLogic.kt` 하나다. 현재 MVP는 그 안의 `increment` 정수 표현식만 수정할 수 있다. 구조·새 실행 코드·계측·fixture·테스트·빌드 설정을 변경하면 빌드 전에 차단한다. 임의의 AI 생성 코드를 호스트 빌드에서 실행하지 않기 위한 의도적인 첫 버전의 제한이다. 범용 코드 수정을 지원하려면 별도 OS/컨테이너 빌드 격리가 필요하다.

수정 후에도 **buggy flavor**를 다시 빌드한다. fixed flavor로 전환해 성공을 만드는 방식은 사용하지 않는다. 원래 실패하던 보호된 `CounterLogicTest`가 통과해야 하며, JUnit XML에 실제 실행된 테스트가 없거나 skip·실패가 있으면 검증이 차단된다. Python 실행기와 판정 코드는 수정 작업 공간 밖에 있다.

최대 3회 패치를 시도하며 같은 패치를 반복하면 중단한다. 에이전트·빌드 명령에는 시간·출력 제한을 적용하고 프로세스 그룹을 정리한다. 네트워크 호출은 `--agent claude`를 선택할 때 발생한다. `--patch-file`은 오프라인 통합 시험용이며 AI 결과로 표시하지 않는다.

## 도구 인터페이스

`python3 -m reproof tools --bundle ... --output ...`는 stdin/stdout JSON-lines 프로토콜이다. MCP 서버 자체는 아니다.

```json
{"tool":"session.inspect"}
{"tool":"device.observe"}
{"tool":"scenario.run"}
```

세션 조회, 진단용 화면 관찰, 고정된 시나리오 실행만 노출한다. 무제한 shell·파일 쓰기는 노출하지 않는다. Claude 패치 어댑터와 이 인터페이스를 통해 다른 에이전트를 연결할 수 있다.

## 현재 범위

실행 가능한 첫 경로는 카운터 이슈의 기록→재현→수정→반복 검증이다. 전체 계획의 모든 단계가 완료된 것은 아니다. 일반 앱 어댑터, 여러 결함 유형·crash 판정, 스크린샷·로그 마스킹, 자동 보관 만료, 여러 기기 큐·웹 UI, 범용 코드 패치 격리는 후속 작업이다. `PLAN.md`는 장기 계약이며 이 문서가 v0.1의 실제 지원 범위를 나타낸다.
