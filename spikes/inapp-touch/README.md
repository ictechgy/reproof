# Spike S2 — 직접 터치 때 앱 안에서 SwiftUI 요소 찾기 (버림용)

설계 문서 7장 spike 2의 측정 코드다. Debug 빌드에 넣은 `TouchProbe`가 `UIApplication.sendEvent(_:)`에서 손을 뗀
터치의 좌표로 앱 안에서 접근성 요소를 찾고, 같은 좌표의 `element_at(실행기 계층)`과 종류·label이 같은지 센다.
결과와 결정은 `docs/superpowers/specs/2026-11-01-ios-commercial-readiness-spike-results.md`의 "S2" 절에 있다.

이 폴더의 코드는 버림용이라 테스트가 없고, 배포 패키지(`reproof*`)에 들어가지 않는다.

## 파일

| 파일 | 역할 |
|---|---|
| `TouchProbe.swift` | 앱 안 프로브(Debug 전용). sendEvent 교체, key window부터 `accessibilityElements`·subview 재귀로 가장 깊은 접근성 요소 찾기, JSONL 기록, 접근성 활성화 방법 1가지, XCUITest 없는 self-check. |
| `TouchProbeBootstrap.m` | `+load`에서 `MixedShopTouchProbe.install`을 이름으로 찾아 부르는 시작 진입점(Debug 전용). |
| `apply.py` | MixedShop을 `~/.reproof/spikes/s2/source/MixedShop`에 새로 복사하고 위 두 파일을 앱 타깃에 넣는다(`examples/`는 고치지 않음). Release 구성에서는 `EXCLUDED_SOURCE_FILE_NAMES`로 뺀다. |
| `measure.py` | 빌드 확인, 측정 실행(`taps`, `selfcheck`), 일치율 표(`report`). |

## 다시 실행하기

준비물: Xcode, iOS Simulator 런타임, XcodeGen. 쓰는 Simulator는 `reproof-mixedshop`뿐이다(run마다 초기화).

```sh
cd <이 저장소>
python3 spikes/inapp-touch/apply.py
python3 spikes/inapp-touch/measure.py build                         # 실행 파일에 프로브 클래스가 있는지 확인
python3 spikes/inapp-touch/measure.py taps --config default         # 약 3분
python3 spikes/inapp-touch/measure.py selfcheck --config default    # 약 1분, taps default의 초기 계층이 필요
python3 spikes/inapp-touch/measure.py selfcheck --config activated
python3 spikes/inapp-touch/measure.py taps --config activated
python3 spikes/inapp-touch/measure.py report                        # 표 출력 + ~/.reproof/spikes/s2/details.json
```

결과물은 모두 `~/.reproof/spikes/s2/` 아래에 생긴다(커밋하지 않음).

- `source/MixedShop` — 프로브를 넣은 사본, `workspace/` — 빌드 작업 공간(`REPROOF_HOME`과 같은 구조)
- `runs/<구성>-<조건>/` — `touch-probe.jsonl`(앱 데이터 컨테이너 `Documents/`에서 복사), `taps.json`(좌표·정답),
  `hierarchies/`(탭 직전 실행기 계층), `session.log`, `meta.json`

## 측정 정의 (측정 전에 정함)

- 프로브 방법(브리프 고정): touch `.ended`의 window 좌표 → 화면 좌표. key window부터 `accessibilityElements`(없으면
  `accessibilityElementCount`/`accessibilityElement(at:)`, 그것도 없으면 subview)를 재귀로 돌아 `accessibilityFrame`이
  좌표를 포함하는 가장 깊은 `isAccessibilityElement == true` 객체를 고른다. 같은 깊이면 나중에 방문한 것.
  숨김·투명·`accessibilityElementsHidden` 객체는 건너뛴다. 원래 sendEvent보다 먼저 해석한다(동작 전 상태).
- A(판정): 프로브가 찾은 요소의 (종류, label) == `element_at`의 (type, label). 종류 대응표는 `measure.py`의
  `CLASS_TYPES`·`TRAIT_TYPES`·`inapp_type`(공개 UIKit 클래스 계통 → trait 순서).
- B(참고, 판정에 안 씀): A에 `element_at`과 같은 승격(가장 깊은 요소가 interactive가 아니면 앱 안 경로에서 가장 가까운
  interactive 조상)을 적용한 값.
- 조건: `taps`는 `RunnerClient.tap_point`(XCUITest가 실제 터치를 넣음, 사람 클릭 대신). `selfcheck`는 XCUITest 없이
  `simctl launch`로 띄우고 프로브가 직접 화면을 옮겨 같은 30개 좌표를 같은 방법으로 해석한다(터치 경로는 거치지 않음).
- 판정 일치율: A 기준, 같은 앱 구성의 `taps`와 `selfcheck` 중 낮은 값. 구성은 기본, 또는 기본 selfcheck에서 SwiftUI
  요소가 안 보이면 활성화 방법 1가지(`TOUCH_PROBE_ACTIVATE_AX=1` → `_AXSSetAutomationEnabled(1)`)를 켠 구성이다.
- 탭 30개: 요약 12(Text 6, 버튼 6), 옵션 18(Toggle 관련 4, Text 5, "추가" 6, TextField 3). Text 12개, 가장자리
  (요소 안쪽 1.5pt) 15개. 목록은 `measure.py`의 `MEASURED_TAPS`.
