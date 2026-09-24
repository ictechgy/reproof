# 실제 기기 Live 입력·스트리밍·worker

2026-09-10 업데이트. Android SM-S947N(API 36)과 iPhone 17 Pro(iOS 27.0) 실기기에서 Live 핵심 경로를 검증했다. iPhone의 세 사례 실제 AI 수정 및 Live 녹화→수정→후보 앱 재개는 [Live repair 문서](LIVE-REPAIR.md)에 기록했다.

## 구현한 경로

```text
Browser → fenced Session API → Android Live provider
                                  ↓ ADB forward + private token
                           persistent UiAutomation helper
                                  ↓
                           actual Android touch + capture
```

원격 구성에서는 provider가 별도 worker 프로세스의 동일 세션 API를 사용한다. 기기 OS lease와 실제 입력은 worker가 소유하고, 상위 controller의 오래된 epoch/sequence/frame을 거절한다.

Android helper는 `android/live`, package `io.reproof.live`, instrumentation `io.reproof.live/.LiveInstrumentation`이다. 기존 `android/driver`의 ID 기반 검증 경로와 분리했다. helper는 자기 앱의 private config 파일에서 임시 토큰을 읽고 바로 지운다. 토큰을 ADB 명령행이나 로그에 넣지 않는다.

## 입력과 미디어

- Android에서 pointer down/move/up/cancel을 실제 발생 시점에 전송한다. 버튼을 놓을 때 전체 gesture를 보내던 기존 모드와 구분한다.
- 최대 5개 pointer ID를 지원하며 실제 기기에서는 2개 동시 pointer 흐름을 검증했다.
- Browser scheduler는 전역 요청 한 개만 전송 중으로 두고, pointer별 최신 move만 보관한다. down/up/cancel은 버리지 않는다.
- 조작권 변경·종료·취소 시 native pointer를 먼저 해제한다. 10초 gesture inactivity는 취소와 epoch 갱신으로 복구한다.
- 회전 후에도 controller가 보낸 cancel은 오래된 frame geometry 때문에 막히지 않는다. 취소 이외의 위치 입력은 geometry와 frame age를 검사한다.
- 알려진 입력 사전조건 거절(예: 포커스된 입력란 없음)은 세션을 유지한다. 주입 결과를 확인하지 못한 경우는 격리한다.
- 미디어는 길이가 표시된 JSON metadata + raw image bytes 스트림이다. Base64 JSON 2Hz polling 제한을 제거했으며, 느린 소비자는 최신 frame만 받는다.
- Android capture 기본값은 JPEG quality 60, 너비 상한 960px, 최대 15FPS다. 이 기기에서 약 14.4FPS를 관측했다. 30/60FPS 영상이나 H.264/WebRTC 구현으로 표현하지 않는다.

서버 frame 한 개는 `uint32BE(metadataLength) + uint32BE(imageLength) + metadata + image`다. metadata는 16KiB, image는 3MiB 이하이며 30초마다 스트림을 종료해 클라이언트가 다시 연결한다. 브라우저는 같은 frame의 픽셀과 metadata를 함께 채택한다.

## 실행

기존에 빌드된 APK를 사용하는 경로:

```bash
python3 -m reproof live-device-doctor
python3 -m reproof live-serve --android auto \
  --android-helper android/live/build/outputs/apk/debug/live-debug.apk \
  --android-app android/sample/build/outputs/apk/buggy/debug/sample-buggy-debug.apk
```

`auto`는 승인된 Android 기기가 정확히 한 대일 때만 선택한다. 여러 대면 serial을 지정한다. helper 옵션을 생략하면 기존 ADB batch baseline을 사용할 수 있다.

새로 빌드할 때:

```bash
cd android
./gradlew --offline :live:assembleDebug :sample:assembleBuggyDebug
```

빌드 의존성이 로컬에 없으면 offline 빌드는 실패한다. 승인받은 의존성 다운로드 후 최신 helper와 buggy/fixed 샘플 빌드 및 fixed 로직 단위 테스트를 완료했다. `/inspect` 목록의 JSON 배열 직렬화 문제도 실기기에서 발견해 수정하고 helper를 다시 빌드했다. 실행한 최종 APK digest는 검증 JSON에 남겼다.

샘플 Activity는 Android 16의 edge-to-edge inset과 가로 화면 배치를 보완해 입력란·버튼이 가려지지 않도록 했다. 기기 방향을 QA 동안 임시 고정하고 원래 설정으로 복원한다. 사용자 앱이나 계정 데이터를 초기화하지 않는다.

## Worker 분리

```bash
python3 -m reproof live-worker --android auto \
  --android-helper android/live/build/outputs/apk/debug/live-debug.apk \
  --android-app android/sample/build/outputs/apk/buggy/debug/sample-buggy-debug.apk \
  --output artifacts/worker --token-stdin

python3 -m reproof live-serve --workers-stdin --output artifacts/coordinator
```

첫 명령의 stdin은 `{ "token": "<private random token>" }`, 두 번째는 다음 구조다. 실제 토큰을 로그나 명령행에 적지 않는다.

```json
{
  "workers": [
    {"id":"usb-worker","url":"http://127.0.0.1:9876","token":"<same private token>"}
  ]
}
```

Worker 기본 bind는 loopback이다. 다른 인터페이스에서 서비스하려면 `--tls-cert`, `--tls-key`가 필요하고 wildcard bind에는 `--advertised-host`를 지정한다. coordinator의 비-loopback URL도 인증서 검증을 하는 HTTPS만 허용한다. 사설 CA는 worker 설정의 `caFile`로 지정한다. 이번 검증은 같은 컴퓨터의 별도 프로세스와 실제 USB 기기를 사용했다. 외부 호스트에 배포하거나 다중 사용자 권한 시스템까지 검증한 것은 아니다.

worker에는 브라우저 UI/CORS, 임의 shell, 임의 파일/설치 명령 API가 없다. 제어 요청은 bearer 인증, 기기 점유, controller epoch, command sequence와 frame mapping을 통과한다. 전송이 끊기면 상위 세션을 격리하며 임의 재시도로 입력을 중복 실행하지 않는다.

## 검증 결과

| 검사 | 결과 |
|---|---|
| 실제 Android 원본 raw-pointer 녹화 재생 | 3/3 `actions_replayed` |
| 실제 드래그 | down + 8 move + up 기록 |
| 동시 pointer | 2개 down/move/up, 종료 후 0개 |
| inactivity | 10초 후 pointer 해제·epoch 증가·수동 제어 복구 |
| 회전 | 오래된 frame의 cancel 허용, 해당 recording 무효화 |
| ADB forward 유실 | 실패 감지 후 인증된 임시 forward로 cleanup, 기기 반납 |
| 브라우저 | up 이전 native pointer 활성 확인, 10개 drag 이벤트 기록 |
| 별도 worker 프로세스 | 실제 기기 녹화/재생, 독립 ID driver로 count `2` 확인, 반납 |
| iPhone HTTP bridge | Simulator에서 인증 거절·idle frame 갱신·4개 입력·정상 종료 |
| iPhone 실기기 | 화면 수신·터치·텍스트·녹화 재생 3/3·스와이프·상세 화면 이동·인증 거절·정상 종료 |

개별 pointer 명령의 실기기 ACK 중앙값은 18ms(17개 표본), 최대값 482ms였다. ADB baseline의 tap ACK 중앙값 304ms와는 동작 단위가 다르므로 직접적인 개선율로 계산하지 않는다. Native 첫 화면 약 2.7초에는 APK 설치와 instrumentation 시작이 포함된다.

증거:

- [Android 종합 QA](../artifacts/android-live-qa/result.json)
- [최신 APK 입력·재생 회귀 QA](../artifacts/android-rebuilt-verified-qa/result.json)
- [최신 APK sample-only 관찰 QA](../artifacts/android-inspect-verified-qa/result.json)
- [실제 worker QA](../artifacts/android-worker-final-qa/result.json)
- [회전·전달 연결 복구](../artifacts/android-recovery-qa/result.json)
- [브라우저 pointer 활성 증거](../artifacts/android-browser-down.json)
- [브라우저 재생 화면](../artifacts/android-browser-replayed.png)
- [브라우저 드래그 화면](../artifacts/android-browser-drag.png)
- [iPhone HTTP bridge의 Simulator QA](../artifacts/iphone-http-bridge-qa-final/result.json)
- [Unsigned iPhone kit](../artifacts/iphone-prepared-final/receipt.json)
- [최초 실기기 서명 kit](../artifacts/iphone-local-signed/receipt.json)
- [현재 Live helper 서명 kit](../artifacts/iphone-live-integrated-build/receipt.json)
- [iPhone 실기기 QA](../artifacts/iphone-physical-qa/result.json)
- [iPhone 재생 후 count 2 화면](../artifacts/iphone-physical-qa/replayed-2.jpg)
- [iPhone 상세 화면 이동](../artifacts/iphone-physical-qa/details.jpg)

실행용 QA 명령은 `scripts/android-live-qa.py`, `scripts/android-inspect-qa.py`, `scripts/android-live-recovery-qa.py`, `scripts/worker-device-qa.py`, `scripts/iphone-bridge-smoke.py`, `scripts/iphone-device-qa.py`에 있다. 각각 새 output 경로를 사용한다.

## iPhone 실기기 실행

```bash
python3 -m reproof live-serve --iphone iphone-0000000000000000 \
  --iphone-products artifacts/iphone-live-integrated-build/runner/Build/Products \
  --iphone-app artifacts/iphone-live-integrated-build/sample/Build/Products/Debug-iphoneos/ReproSample.app \
  --output artifacts/iphone-physical-console
```

이 public 기기 ID는 검증에 사용한 iPhone용이다. 다른 기기에서는 `live-device-doctor` 결과를 사용하고 해당 기기를 포함하는 개발 프로파일로 서명해야 한다.

승인 후 서명 환경을 확인했다. Xcode에 로그인된 계정이 없어 자동 프로비저닝은 실패했지만, 현재 기기와 모든 테스트 앱 ID를 허용하는 유효한 와일드카드 개발 프로파일 및 일치하는 인증서가 로컬에 있었다. 현재 소스와 일치하는 unsigned kit를 별도 경로에 복사하고, 내장 코드부터 서명한 뒤 프로파일과 앱별 entitlement를 적용했다. `codesign --verify --deep --strict`와 실제 설치·XCTest 실행이 통과했다. 서명된 앱은 개발 프로파일 유효기간과 등록 기기에 종속된다.

실기기 QA는 원본 4개 입력(입력란 tap, text, Done tap, Add tap)을 녹화하고 reset을 포함해 3회 재생했다. 세 번 모두 `actions_replayed`를 확인했으며 두 번째·세 번째 재생 화면에서 `QA`와 count `2`를 확인했다. 녹화에는 텍스트 원문 대신 변수만 남는다. 최종 측정은 0.77FPS, 직접 입력 ACK 1,046–1,382ms였다. 이때 미리보기에서 글자가 누락된 것으로 판단했으나, 이후 원본 픽셀·OCR·PNG 디코딩으로 정상임을 확인해 정정했다. 최초 QA는 명령 재생과 수동 화면 대조였으며, 후속 보호된 XCTest의 실기기 검증 결과는 LIVE-REPAIR.md를 따른다.

후속 간격 조정에서는 대기 중 약 3FPS, 같은 입력·재생 QA에서는 0.78→0.92FPS를 관측했다. iPhone 입력은 XCTest의 tap/long-press/swipe/text batch 경로다. Android의 지속적인 pointer down/move/up 또는 동시 멀티터치와 같은 지원 범위로 표시하지 않는다. 테스트 종료 시 native runner가 정상 종료하고 기기를 반납했다.

## 남은 경계

최신 helper의 sample-only `/inspect` endpoint는 실기기 검증을 완료했다. 허용한 샘플 노드의 ID·좌표·상태, 입력란의 텍스트 유무, 숫자 count만 반환한다. 입력 문자열은 반환하지 않는다. 기존 ID driver와 화면 대조 검증 근거도 보존했다.

새 iPhone 소스 빌드는 `live-iphone-build --team <TEAM_ID>`로 서명할 수 있다. 자동 프로비저닝에는 Xcode 계정 설정이 필요하며 `--provision`은 Apple portal 접근을 명시적으로 허용할 때만 사용한다. 이번 실기기 검증에서는 사용자 승인 아래 기존 프로파일·Keychain 인증서를 사용했다. 기존 Simulator 방식도 유지한다.

이 결과는 실제 Android Live와 worker 실행 기반의 진전이다. WebDriver/Appium 전체 호환, 역할별 다중 사용자 운영, 30/60FPS 영상, 실제 화면 AI와 기존 repair 엔진 전체의 세션 서비스 통합을 완료한 것으로 표시하지 않는다.
