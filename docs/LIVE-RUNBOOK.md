# Live 세션 실행과 검증

2026-09-09. STF 코드 의존성이 없는 로컬 단일 사용자 구현이다. 브라우저 화면 → 좌표 입력 → 실제 기기 acknowledgment → 녹화 → 재생을 공통 세션으로 연결한다. 전체 플랫폼 P1–P6 완료를 의미하지 않는다.

## 실행

저장소 루트에서 실행한다. 런타임 Python 외부 패키지와 npm 설치는 필요 없다.

```bash
# 합성 기기: 서버·UI·녹화 흐름을 로컬에서 확인
python3 -m reproof live-serve --demo
```

서버가 출력하는 `http://127.0.0.1:8765`를 연다. `localhost` 등 다른 Host는 거절한다. 기기 선택 → Start session → Reset & record → 화면 조작 → Stop → Replay recording 순서다. 텍스트를 보내기 전에 기기 화면의 입력란에 포커스를 둔다. 녹화의 문자열은 변수로 치환되므로 재생 전에 변수 값을 입력한다.

실제 iOS Simulator는 Xcode와 설치된 target app이 필요하다. 기존 샘플 빌드는 [iOS 실행 문서](IOS-RUNBOOK.md)를 따른다. Live driver는 기존 replay runner와 별도 프로젝트다.

```bash
xcodebuild build-for-testing \
  -project live-ios/ReproLive.xcodeproj -scheme ReproLive \
  -configuration Debug -sdk iphonesimulator \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath live-ios/build -disableAutomaticPackageResolution \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO

# 사용할 Simulator를 먼저 부팅하고 샘플을 설치한다.
xcrun simctl boot <SIMULATOR_UUID>
xcrun simctl install <SIMULATOR_UUID> <ReproSample.app 경로>
python3 -m reproof live-serve --simulator <SIMULATOR_UUID> --demo
```

이 작업에서 검증한 Simulator UUID는 `D3A74A3D-F247-498C-AA4D-33891901E946`, 설치 앱은 `artifacts/ios-cases-build/DerivedData/Build/Products/Debug-iphonesimulator/ReproSample.app`이다. 이미 부팅된 Simulator에 `boot`를 다시 실행하지 않는다.

`--bundle`로 설치된 다른 앱을 선택할 수 있으나, 단순 앱 재실행은 재현 가능한 상태 복원으로 인정하지 않는다. 현재 재생 가능한 iOS reset fixture는 샘플의 `REPRO_MODE=replay`, `REPRO_CASE=counter`다. 앱 artifact digest가 달라지면 기존 녹화를 같은 앱으로 재생하지 않는다. 샘플의 iPhone 실기기 서명·Live 입력·재생 검증은 DEVICE-LIVE.md에 기록했다. 일반 앱 호환은 검증하지 않았다.

Android는 연결되고 승인된 기기의 serial을 명시한다.

```bash
python3 -m reproof live-serve --android <ADB_SERIAL> --demo
```

Android baseline은 PNG 캡처와 ADB batch tap/long-press/swipe/home을 제공한다. 텍스트·fixture reset·재생은 광고하지 않는다. 화면 회전은 다음 frame의 geometry로 감지한다. 연결 해제 시 세션을 실패/격리하고 정상 정리 후 다시 연다. 현재 Android 기기가 없어 이 경로는 명령 변환 테스트만 통과했으며, 연속 입력 helper와 실제 성능 검증은 남아 있다.

## 저장·내보내기·자동화

기본 저장 위치는 `artifacts/live`이며 `--output`으로 바꾼다. 완료 녹화는 `recordings/<id>.json`, 재생 영수증은 `replays/<id>.json`에 저장한다. 서버 재시작 후 완료 녹화를 다시 읽는다. 진행 중인 세션과 녹화는 재시작 복구 대상이 아니다.

- Export JSON: 타이밍, 정규화 좌표, frame/geometry 참조, 조작 주체와 변수 목록.
- Export Python replay: 녹화 ID와 digest를 고정한 Python 실행 스크립트. 원래 녹화가 같은 서버 저장소에 있어야 한다.
- Python 스크립트는 새 세션을 만들고 정리한다. `--session`을 주면 기존 세션의 조작권을 받아 실행한 뒤 이전 controller에게 돌려준다. 별도의 사람/AI 입력 통로를 만들지 않는다.

```bash
# 저장소 루트에서 실행. 텍스트 변수는 echo 없이 터미널에서 입력한다.
PYTHONPATH=. python3 /path/to/replay-<id>.py --server http://127.0.0.1:8765
PYTHONPATH=. python3 /path/to/replay-<id>.py --session <SESSION_ID>
```

녹화 라이브러리·검증된 JSON 가져오기·속도 조절 복사·작업 큐·CLI는 [Live 운영 문서](LIVE-OPERATIONS.md)에 추가했다. 현재는 동일 기기·동일 앱 artifact·동일 화면 크기/방향의 좌표 재생이다. 의미 selector 변환, 다른 기기 적응형 재생, Appium/WebDriver 호환 스크립트는 포함하지 않는다. `actions_replayed`는 주입된 입력들이 완료됐다는 뜻이다. 원래 버그 재현이나 수정 성공 판정은 기존 oracle/repair 경로와 별도다.

브라우저 새로고침은 같은 탭의 세션 ID를 복구한다. `?session=<id>`로 기존 세션을 관찰하고 Take control로 조작권을 인계할 수 있다. 새 controller epoch가 발급되면 이전 controller의 대기 입력은 거절된다.

## 구현 계약과 제약

| 항목 | 현재 구현 |
|---|---|
| 운영 범위 | `127.0.0.1` 단일 사용자, host/origin 검사·HttpOnly SameSite cookie, native bridge 별도 임시 인증 |
| 기기 점유 | 세션별 독점 배정, 기존 ADB/Simulator 배치 실행과 같은 OS lease |
| iOS 입력 | 세션당 지속 XCUITest 1개, tap/long press/swipe/text/home/reset |
| 미디어 | 최신 JPEG/PNG frame만 메모리 유지, 브라우저 최대 2Hz polling, 실제 새 frame FPS 표시 |
| 입력 | pointer를 놓을 때 gesture-batch 전송, sequence·command ID·controller epoch·frame age·geometry 검사 |
| 재생 | acknowledgment 기반, 원래 dispatch 간격, 완료 녹화 무결성 검사, 취소·인계 |
| 녹화 | 최대 500개 입력/10분, 문자열은 변수화, 프레임 바이트/영상은 저장하지 않음 |
| 종료 | native runner 정상 종료 확인, 불확실한 입력/초기화/정리는 격리 |

고FPS 영상, WebRTC, 누르는 동안의 연속 drag, 멀티터치, IME 조합, 외부 터치 관측, 자동 앱 설치 UI, 멀티호스트·다중 사용자 권한은 후속 구현이다. 단일 host의 작업 큐와 세션 수명 관리는 [운영 문서](LIVE-OPERATIONS.md)를 따른다. iOS driver는 15분 제한이며, 종료되면 세션을 닫고 다시 연다. 앱의 외부 서버 상태·계정·Keychain을 초기화하지 않는다.

최신 화면은 메모리만 보관한다. XCTest가 자체 생성하는 결과는 작업 전용 임시 디렉터리에 두고 세션 정리 시 삭제한다. 프로세스 강제 종료로 정리가 실행되지 않으면 임시 결과가 남을 수 있다. 화면에 입력 문자가 나타날 수 있으므로 현재 콘솔을 인터넷에 노출하는 배포 구성으로 사용하지 않는다. API/프로세스 로그에는 입력 문자열과 인증값을 출력하지 않는다.

## 검증

```bash
python3 -m unittest discover -s tests
node --check live-web/app.js
python3 scripts/live-smoke.py \
  --device ios-<소문자 SIMULATOR_UUID> \
  --output artifacts/live-smoke-new
```

smoke는 설치된 합성 카운터 앱에 API를 통해 좌표 탭·텍스트 입력·Done·Add를 보내 녹화/재생하고 각각 JPEG를 남긴다. 브라우저의 사람 조작과 구분해 `scripted-console-input`으로 기록한다. 실행 결과와 시각 증거는 [Live 검증 보고서](LIVE-QA.md)에 정리한다.


## 실제 기기 확장

위의 batch/polling 설명은 초기 baseline 범위다. Android continuous-pointer, binary stream, 별도 worker와 iPhone 실기기 서명·재생 결과는 [최신 기기 Live 문서](DEVICE-LIVE.md)를 따른다.

샘플 iPhone의 Live 녹화→실제 AI 수정→검증된 앱 재개는 [Live repair 문서](LIVE-REPAIR.md)를 따른다. 서버에서 보호된 소스·빌드와 `--repair-agent claude`를 명시해야 활성화된다.
