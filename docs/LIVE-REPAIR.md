# iPhone Live 녹화에서 AI 수정·실기기 검증까지

Android 카운터의 같은 통합 흐름도 검증했다. 실행 명령과 근거는 [Android Live repair](ANDROID-LIVE-REPAIR.md)를 참고한다.

2026-09-10. iPhone 17 Pro(iOS 27.0)에서 세 가지 샘플 버그의 실제 Claude 수정과 검증을 완료했다. 브라우저 Live에서 녹화한 카운터 버그를 같은 서버의 수정 작업으로 넘기고, 검증된 앱을 다시 열어 count `1`을 확인했다.

## 검증한 결과

| 경로 | 원본 재현 | 보호된 로직 테스트 | 수정본 실기기 검증 |
|---|---:|---:|---:|
| counter | 3/3 | 1 통과 | 3/3 |
| duplicate-submit | 3/3 | 1 통과 | 3/3 |
| reset | 3/3 | 1 통과 | 3/3 |
| 브라우저 Live counter → 실제 Claude 수정 | 3/3 | 1 통과 | 3/3 |

각 작업은 실제 Claude 요청을 사용했다. 후보마다 원본 테스트 runner를 그대로 사용하며, 모델은 허용한 제품 파일의 제한된 표현식만 수정할 수 있다. 원본 소스·테스트·fixture·oracle는 보존했다. 패치는 각 작업의 `patch.diff`와 별도 후보 디렉터리에 있다.

- [세 사례 종합 보고서](../artifacts/iphone-repair-cases/index.html)
- [세 사례 검증 JSON](../artifacts/iphone-repair-cases/validated-summary.json)
- [Live에서 시작한 수정 보고서](../artifacts/iphone-live-repair-verified/repairs/4e8162c0d6914d059ada77e364604972/repair/report.html)
- [Live 원본 화면](../artifacts/iphone-live-repair-before.png)
- [검증된 앱을 다시 연 화면](../artifacts/iphone-live-repair-after.png)
- [수정본 count 1 원본 캡처](../artifacts/iphone-live-repair-final/frame.png)
- [통합 경로 검증 JSON](../artifacts/iphone-live-repair-final/result.json)

## 현재 빌드로 실행

```bash
python3 -m reproof live-serve \
  --iphone iphone-0000000000000000 \
  --iphone-products artifacts/iphone-live-integrated-build/runner/Build/Products \
  --iphone-app artifacts/iphone-repair-original-build/DerivedData/Build/Products/Debug-iphoneos/ReproSample.app \
  --repair-source ios \
  --repair-build artifacts/iphone-repair-original-build \
  --repair-agent claude \
  --output artifacts/iphone-repair-console
```

브라우저에서 기기를 선택하고 세션을 연다. **Reset & record** 후 입력란에 `QA` 또는 `Test`를 입력하고 Done → Add를 누른다. Stop 후 **Analyze & repair recording**을 누른다. 작업이 `verified`가 되면 **Open repaired app**으로 후보 앱을 연다.

`--repair-agent claude`는 명시적인 외부 모델 사용 설정이다. 이 설정 없이 서버를 실행하면 수정 API는 비활성화된다. 요청은 관리자가 지정한 소스·빌드만 사용하며 HTTP 요청이나 에이전트 도구에서 임의 소스 경로·외부 모델 주소를 지정할 수 없다.

새 빌드는 다음처럼 준비한다. `--iphone`은 `live-device-doctor`에 표시되는 public 기기 ID다. 각 output에는 새 경로를 사용한다.

```bash
python3 -m reproof ios-build \
  --iphone iphone-0000000000000000 --source ios \
  --output artifacts/new-iphone-build

python3 -m reproof ios-record \
  --iphone iphone-0000000000000000 --case counter \
  --build artifacts/new-iphone-build --output artifacts/new-counter

python3 -m reproof ios-repair artifacts/new-counter/bundle \
  --iphone iphone-0000000000000000 --source ios --agent claude \
  --output artifacts/new-counter-repair
```

`ios-build --iphone`은 로컬 개발 프로파일·Keychain을 사용하며 Apple portal을 호출하지 않는다. 유효기간, 등록 기기, 앱 ID 범위, 사용 가능한 서명 인증서를 확인하고 고정된 샘플/test 앱만 서명한다. 프로파일이 없으면 준비 단계에서 중단한다. Simulator 명령은 기존 `--simulator` 옵션을 유지한다.

## 연결 방식과 검증 기준

1. Raw Live 녹화와 같은 앱 실행에서 샘플 SDK가 기록한 의미 기반 이벤트를 함께 수집한다. SDK는 빈 문자열·`QA`·`Test` 외의 텍스트를 기록하지 않고 캡처를 무효화한다.
2. Mac의 Vision OCR로 현재 샘플 화면의 count를 관찰한다. 샘플 배치의 count 영역을 사용하며, 판단이 불분명하거나 선택한 버그 값과 다르면 수정 요청을 거절한다. 일반 화면 AI 탐색기가 아니다.
3. 마지막 녹화 이후 추가 입력·앱 reset·앱 artifact 변경·원본 소스 변경을 거절한다. 녹화 ID/digest와 화면 frame ID를 작업에 연결한다.
4. Live runner를 정상 종료하고 기기를 수정 작업에 예약한다. 원본 세션 ID는 작업에 보존한다. 실제 SDK 이벤트로 만든 고정 bundle로 원본을 3회 재현한다.
5. Claude에 허용한 제품 소스와 QA 이벤트·oracle만 전달한다. 화면 이미지는 로컬에 남는다. 후보 앱을 빌드·서명하고 보호된 로직 테스트와 실기기 3회 재검증을 수행한다.
6. 검증된 후보만 다시 열 수 있다. 후보를 여는 Live 세션은 새 ID를 가지며 원본 작업과 연결된다. 원본 작업 소스가 자동으로 덮어써지는 것은 아니다.

취소하면 수정 자식 프로세스에 중단을 전달하고 종료를 기다린다. 정리를 확인하지 못하면 기기를 격리한다. 서버 재시작 후 진행 중 작업을 자동 재개하지 않는다. 검증 이력만 남아 있고 해당 후보가 서버에 설정되지 않았다면 원본 앱을 후보인 것처럼 열지 않는다.

HTTP API는 `/api/repairs`, `/api/repairs/{id}`, 세션의 `/repair`, 작업의 `/cancel`, `/resume`, `/report`다. JSON-lines 도구도 `repairs.list/get/submit/cancel/resume`을 제공한다. 기존 loopback·same-origin·owner·controller/epoch 검사를 사용한다.

## 캡처 판단 정정과 성능

앞서 보고한 iPhone 글자 누락은 잘못된 판단이었다. 같은 원본 JPEG에서 픽셀과 OCR로 글자가 존재함을 확인했고, PNG로 디코딩한 화면도 정상이었다. PNG/JPEG 비교 36장과 추가 Live 재생에서도 누락을 확인하지 못했다. 기기 캡처 결함으로 기록했던 내용을 정정한다.

- [원본 픽셀 검사](../artifacts/iphone-physical-qa/pixel-check.json)
- [같은 원본 JPEG를 디코딩한 PNG](../artifacts/iphone-physical-qa/replayed-1-decoded.png)
- [PNG/JPEG 비교](../artifacts/iphone-capture-formats/text-presence.json)
- [좌표 입력 조건 비교](../artifacts/iphone-capture-coordinate-verified/text-presence.json)

캡처 간격을 0.5→0.2초, 명령 확인 간격을 0.15→0.05초로 조정했다. 같은 18개 입력·재생 QA의 최종 frame-window 측정은 0.78→0.92FPS였고, 직접 입력 6개의 ACK 중앙값은 1,192.5→1,034.5ms였다. 대기 중에는 호스트 약 2.96FPS, 브라우저 약 2.9FPS를 관측했다. 이 수치는 작은 로컬 표본이며 30/60FPS 영상 성능을 의미하지 않는다.

- [변경 전 QA](../artifacts/iphone-live-baseline-rerun/qa/result.json)
- [변경 후 QA](../artifacts/iphone-live-faster-qa/qa/result.json)

브라우저에서 시작할 때 CoreDevice 터널 주소가 서버 등록 당시와 달라지는 문제도 발견했다. 앱 설치 직후 같은 기기의 연결 정보를 새로 확인하도록 수정했다. 이후 원본 Live 세션과 검증된 후보 Live 세션 모두 정상 연결됐다.

## 범위

현재 통합 UI의 실기기 검증은 iPhone 세로 화면의 카운터 샘플이다. CLI의 실제 AI 수정 검증은 세 사례 모두 완료했다. `--iphone-case`로 샘플 fixture를 선택할 수 있으며, 다른 fixture의 raw 녹화를 혼용하지 않도록 provider 종류를 구분한다.

SwiftUI·일반 앱의 임의 수정, iPhone 연속 pointer/멀티터치, 외부 worker에서의 수정 작업, 다중 사용자 권한은 이번 완료 범위에 포함하지 않는다. XCTest gesture-batch 입력과 sampled JPEG의 한계도 유지된다.
