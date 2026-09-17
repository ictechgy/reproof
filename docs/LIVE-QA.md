# Live 검증 — 2026-09-09

## 실행한 범위

로컬 Python HTTP 서버, vanilla browser console, 지속 XCUITest driver와 실제 iOS Simulator를 연결했다. 기존 Android/iOS SDK·재현·Claude repair 코드는 변경하지 않았다. CLI에 `live-serve` 분기만 추가했다.

- Python 단위/계약/HTTP/자동화 테스트 77개 통과. 기존 60개와 Live 관련 17개다.
- native Live driver의 Xcode build-for-testing 성공.
- 추가 실제 iOS 검사: long press·swipe·Home·reset 모두 주입 확인, 정상 종료. [영수증](../artifacts/live-gestures.json).
- 브라우저 실제 iOS 화면에서 tap → `QA` text → Done tap → Add tap 녹화/재생. 4개 입력 모두 `injected`, 재생 결과 `actions_replayed`, 두 화면 모두 카운터 `2` 확인.
- 최종 native 드라이버와 서버로 API smoke 재실행. 앱 artifact digest 고정, 입력 acknowledgment와 새 frame, 4개 입력 재생, 세션 정상 종료 검사.
- 생성한 Python 스크립트를 별도 Python 프로세스에서 실행하고 합성 기기 새 세션 생성·재생·반납까지 검사.
- 브라우저 새로고침 후 세션 복구 확인. 1600×1100과 390×844 화면 확인.
- Android ADB 좌표 변환/미지원 입력 거절은 synthetic transport 테스트. 실제 Android가 없어 기기 검증은 하지 못함.

## 증거

| 증거 | 위치 |
|---|---|
| 실제 browser 조작 녹화 화면 | [recorded](../artifacts/live-ios-recorded.png) |
| 실제 browser 조작 재생 화면 | [replayed](../artifacts/live-ios-replayed.png) |
| 최종 iOS API smoke 영수증 | [result.json](../artifacts/live-final-verified/result.json) |
| 최종 녹화/재생 이미지 | [recorded.jpg](../artifacts/live-final-verified/recorded.jpg), [replayed.jpg](../artifacts/live-final-verified/replayed.jpg) |
| 모바일 UI — 합성 기기 | [mobile](../artifacts/live-mobile.png) |

브라우저 녹화는 초기 통합 revision의 증거이며 최종 API smoke를 별도로 남겼다. 최종 테스트 수와 명령 출력은 `artifacts/live-validation.json`에 저장한다. 입력 영수증은 앱 결과 oracle을 대신하지 않는다. 이 QA는 합성 샘플 화면을 눈으로 대조했으며 일반 앱의 자동 버그 판정까지 검증한 것은 아니다.

## 확인한 실패 경계

중복 device 배정, 다른 owner 접근, 이전 controller epoch, 동일 command ID의 중복 주입/다른 payload 재사용, geometry 변경, 모호한 input/reset 결과 격리, 제어권 인계로 재생 취소, 완료 녹화 변조, 앱 identity 변경, 빈 녹화 재생, text 원문 보관 방지, Host/Origin/Content-Type 검사, 종료된 세션 입력 거절을 검사한다.

현재 수치는 성능 SLA가 아니다. 첫 실제 API smoke에서 첫 frame까지 약 7.9초, 네 입력의 acknowledgment 626–958ms였다. screenshot 캡처와 XCUITest 대기가 포함된 소규모 관측이다. Android 기준선, 연속 입력 지연 p50/p95, 연결 복구 SLA와 부하 검사는 남아 있다.

## 완료 수준

P0 공통 세션과 iOS 지속 제어의 실행 가능한 구현, P3 Simulator 일부 경로를 검증했다. P1 Android 연속 입력, P2 다중 worker 팜, P4 WebDriver/CI 호환, P5 화면 AI, P6 기존 repair의 세션 통합은 완료 처리하지 않는다. [실행 문서](LIVE-RUNBOOK.md)의 capability 표를 실제 지원 범위로 사용한다.
