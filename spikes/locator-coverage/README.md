# Spike S1 — SwiftUI 요소 식별 측정 (버림용)

설계 문서 7장 spike 1의 측정 스크립트다. 런타임 접근성 방식(`reproof/simple/locator.py`가 실행기 계층에서 요소를
찾는 방식)이 같은 화면의 다른 실행과 AI 수정 뒤 빌드에서 요소를 맞게 찾는지 센다. 결과와 결정은
`docs/superpowers/specs/2026-11-01-ios-commercial-readiness-spike-results.md`의 "S1" 절에 있다.

이 폴더의 코드는 버림용이라 테스트가 없고, 배포 패키지(`reproof*`)에 들어가지 않는다.

## 파일

| 파일 | 역할 |
|---|---|
| `edits.py` | MixedShop 사본을 만들고 AI 수정 흉내 변경을 적용한다(`examples/`는 고치지 않음). |
| `collect.py` | `reproof app inspect`와 같은 순서(빌드 재사용 → 전용 Simulator 초기화·설치 → 실행기 → 앱 실행)로 앱을 띄우고, 탭으로 화면을 옮겨 가며 계층 JSON을 저장한다. |
| `coverage.py` | 같은 화면의 두 계층에서 `capture_evidence` → `resolve` 결과를 분류해 Markdown 표로 낸다. |

## 다시 실행하기

준비물: Xcode, iOS Simulator 런타임, XcodeGen(MixedShop), S3 spike에서 받은 Wikipedia 소스
`~/.reproof/sources/wikipedia-ios`(`reproof.toml`과 결과 문서 S3의 "검증 앱 설정" 2건 포함). 쓰는 Simulator는
`reproof-mixedshop`, `reproof-wikipedia-ios`뿐이다.

```sh
cd <이 저장소>
python3 spikes/locator-coverage/edits.py                      # 변형 소스 6개
python3 spikes/locator-coverage/collect.py mixedshop          # baseline run-a, run-b
for v in edit-above edit-other edit-action edit-all ui-insert; do
  python3 spikes/locator-coverage/collect.py mixedshop --variant "$v" --runs run-a
done
python3 spikes/locator-coverage/collect.py wikipedia          # run-a, run-b (run마다 약 9분, 대부분 idle 대기)
python3 spikes/locator-coverage/coverage.py                   # 표 출력 + coverage-details.json
```

결과물은 모두 `~/.reproof/spikes/locator-coverage/` 아래에 생긴다(커밋하지 않음).

- `sources/<변형>/MixedShop` — 변형 소스 사본
- `workspace/` — MixedShop 빌드 작업 공간(`REPROOF_HOME`과 같은 구조). Wikipedia는 기존 `~/.reproof` 빌드를 재사용한다.
- `dumps/<앱>/<변형>/<run>/<화면>.json` — 실행기 계층 문서 그대로, `meta.json`(빌드 ID, 단계별 시간), `session.log`
- `coverage-details.json` — 요소별 분류 기록

## 측정 정의

- 대상 요소: `element_type`이 `INTERACTIVE_TYPES`(`reproof/simple/hierarchy.py`)인 요소. `staticText`는 판정(oracle)
  후보라 따로 센다.
- 실행 간 안정성: 같은 빌드를 run-a, run-b로 각각 새로 초기화·설치·실행해 같은 화면 계층을 얻고, A의 요소마다
  A에서 근거를 뽑아 B에서 해석한다.
- AI 수정 뒤 유지율: baseline run-a의 근거를 변형 빌드 run-a 계층에서 해석한다. 유지율 = 맞게 찾은 수 / B에 정답
  요소가 있는 수.
- 정답 요소(counterpart)와 분류 규칙은 `coverage.py` 맨 위 설명에 있다. 요약하면 정답은 identifier(유일할 때) →
  서명(종류, identifier, label, 감싼 `cell` 안의 글자)이 같은 요소 → 같은 서명·frame → 같은 path·종류·label 순서로
  정하고, 해석 결과의 path가 정답과 다르면 "잘못 해석"이다. 표의 굵은 합계가 규칙 그대로(모든 화면)의 값이다.
  Wikipedia의 온보딩 관심사·홈을 뺀 합계(`NETWORK_SCREENS`)는 결과를 본 뒤 정한 사후 분류라 참고로만 보인다.
- `collect.py`는 실행기 명령 대기 시간을 300초로 늘린다(`client._timeout`). Wikipedia 홈처럼 idle이 되지 않는 화면은
  XCUITest가 탭 앞뒤로 60초씩 기다려 기본값 30초로는 응답 전에 끊기기 때문이다. 결과 문서의 발견 항목에 적었다.
