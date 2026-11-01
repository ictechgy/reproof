# iOS 상용 준비 spike 결과

설계 문서(`2026-11-01-ios-commercial-readiness-design.md`) 7장의 spike 결과를 모은다. 모든 수치는 실제 실행에서
측정했고, 추정이 섞인 곳은 "추정"으로 표시한다.

## S1 — SwiftUI 요소 식별

### 판정

**고정 규칙을 문자 그대로 적용하면 실패다(83.7%/84.8%, 잘못 해석 12건).** 실패 원인은 네트워크 콘텐츠 화면(온보딩
관심사)의 무작위 목록 행이다. 소스 재작성으로도 이 경우는 구별되지 않는다고 판단한다(같은 `ForEach` 호출 위치의 행은
같은 자동 ID를 받는다). 이것은 추론이며 측정하지 않았다. **그래서 계획 2는 런타임 접근성 방식으로 잠정 진행한다.**
사용자가 원하면 SwiftSyntax 다운로드를 승인해 재작성 방식을 따로 측정할 수 있다(미승인, 이번에는 받지 않았다).

| 조건 | 기준 | 규칙 그대로(모든 화면 13개) | 홈만 제외(온보딩 관심사 포함, 12개) | 네트워크 화면 2개 사후 제외(참고, 11개) |
|---|---|---|---|---|
| strong + `type-label-anchor` + `label-ordinal` 비율 | 90% 이상 | 139/164 = 84.8% ✗ | 128/153 = 83.7% ✗ | 86/86 = 100% |
| 잘못 해석 | 0건 | 12건 ✗ | 12건 ✗ | 0건 |
| AI 수정 뒤 유지율 | 95% 이상 | 세 종류 각각 23/23 = 100% ✓ (구성상 항상 통과: 화면에 보이지 않는 수정) | 같음 | 같음 |

- 온보딩 관심사는 브리프의 "첫 실행 온보딩" 범위 안이다. 네트워크 화면 2개를 빼는 분류(`NETWORK_SCREENS`)는 잘못 해석
  12건을 본 뒤에 정한 **사후 분류**라, 오른쪽 열은 판정에 쓰지 않고 참고로만 둔다. 홈은 계층을 읽은 시점의 피드 로딩
  정도가 run마다 달라(73개, 157개) 이 비교에는 맞지 않지만, 홈을 넣든 빼든 결과는 실패로 같다.
- 잘못 해석 12건은 run-a에 있던 무작위 문서 버튼이 run-b에서 사라졌는데, 버튼 수가 같아서 `position-ordinal`이 같은
  순번의 다른 문서를 고른 것이다.
- 유지율 23/23은 판정 근거가 되지 못한다. 세 종류 수정은 모두 화면에 보이지 않아 계층이 baseline과 내용상 같았고, 그러면
  locator는 항상 통과한다. 실제 AI 수정은 보이는 UI를 바꾸는 경우가 많다. 이번 측정에서 정보가 있는 유지율 값은 보이는
  버튼·행을 하나 끼운 참고 변형 `ui-insert`의 17/23(잘못 해석 0, 실패 6은 모두 안전한 실패)뿐이다.
- 설계 7장 spike 1의 "외부 Swift 패키지 캐시·오프라인 재빌드"는 잠정 결정에서는 해당 없다. 재작성 방식을 측정하기로
  하면 그때 정한다.

숫자를 읽을 때 주의할 점:

1. **로컬 화면의 100%는 구성상 나온 값이다.** 로컬 화면 11개의 계층 JSON은 두 실행(매번 Simulator 초기화·새 설치)에서
   내용이 완전히 같았고(키 순서만 다름), AI 수정 세 종류로 다시 빌드한 MixedShop의 세 화면 계층도 baseline과 같았다.
   트리가 같으면 locator는 항상 같은 요소를 고른다. 따라서 이 측정이 실제로 보여 준 것은 다음 세 가지다.
   - 고정된 Simulator 설정(초기화, 로케일, 상태 바)에서 로컬 화면 계층은 결정적이다.
   - 화면에 보이지 않는 소스 수정(위에 20줄 추가, 다른 버튼 로직, 대상 action 변경)은 런타임 근거를 전혀 바꾸지
     않는다. 설계 6장의 "줄 번호가 바뀌어도 같은 ID" 조건은 런타임 방식에서는 처음부터 문제가 되지 않는다.
   - 로컬 화면에서는 측정한 모든 요소가 하나로 특정됐다(실패 0).
2. **약점: 로컬 화면 86개 중 42개(49%)는 label이 빈 요소다.** SwiftUI List 행의 `cell`, `labelsHidden()` 토글의
   `switch`, 입력 칸이 여기에 해당하고, 이들은 "빈 label 요소 중 몇 번째"라는 순번으로만 찾았다. 규칙상
   `label-ordinal`로 셌지만 실제로는 위치 근거다. Wikipedia SwiftUI 화면의 `label-ordinal` 34건은 모두 빈 label이었다.
   `ui-insert`(행 하나 끼움)에서 이 `cell` 6개는 모두 "실패"였는데, 행 수가 6개에서 7개로 바뀌어 순번 근거를 건너뛰었기
   때문이다.
3. **순번 근거는 후보 수가 바뀔 때만 안전하게 실패한다.** `label-ordinal`과 `position-ordinal`은 녹화 때와 후보 수가
   다르면 건너뛰지만, 수가 같은 채 내용이 바뀌거나 순서가 바뀌면 다른 요소를 고른다. 위 12건이 바로 이 경우다.

### 계획 2 지침

- 빈 label로 찾은 `label-ordinal` 결과는 `position-ordinal`과 똑같이 다룬다. 둘 다 "확인 필요"로 보고하고 통과로 세지 않는다.
- 위 3처럼 두 순번 근거는 후보 수가 바뀔 때만 안전하게 실패하고, 수가 같은 교체·재정렬에는 속는다(잘못 해석 12건이 이
  경우). 따라서 후보 수가 녹화 때와 같다는 사실만으로 순번 결과가 맞다고 보지 않는다. label이 있는 `label-ordinal`도 설계
  6장대로 "약한 근거"로 따로 센다.
- 네트워크 콘텐츠에 기대는 화면을 재생하려면 결정적인 시작 조건(고정된 콘텐츠: 고정 응답, 시드 데이터 등)이 필요하다. 그런
  조건이 없으면 그 화면의 단계는 재생 대상에서 빼거나 "확인 필요"로 둔다.
- 유지율은 화면에 보이는 UI를 바꾸는 수정(`ui-insert` 같은 경우)으로 다시 재야 의미가 있다.

### 측정 방법

- 환경: S3과 같은 Mac(macOS 26.7.2, Xcode 27.0, iOS 27.0 런타임, Python 3.14.7). Simulator는 `reproof-mixedshop`,
  `reproof-wikipedia-ios`만 썼다.
- 스크립트: `spikes/locator-coverage/`(버림용, README에 다시 실행하는 법). `collect.py`는 `reproof app inspect`와 같은
  함수를 같은 순서로 쓴다(완성 빌드 재사용 → 전용 Simulator 초기화·표시 고정·설치 → 실행기 → 로케일 인자로 앱 실행).
  그 뒤 요소를 label·identifier로 찾아 `RunnerClient.tap_point`로 탭하며 화면을 옮기고 계층을 저장한다. run마다
  Simulator를 초기화하므로 run-a와 run-b는 서로 독립된 새 설치·첫 실행이다. 계층 JSON은 `~/.reproof/spikes/`에만 있다.
- 빌드: MixedShop은 `examples/` 사본 6개(baseline + 변형 5개)를 `~/.reproof/spikes/locator-coverage/workspace`에
  `prepare_build`로 빌드했다(빌드 ID 6개가 서로 다름, 각 빌드 폴더 사본에 변경이 들어갔는지 확인). Wikipedia는 S3의 빌드
  `80392764bf5f8c1b`를 재사용했다.
- 화면(괄호는 SwiftUI 여부, 소스로 확인):
  - MixedShop: 장바구니(UIKit), 옵션(UIKit에 끼운 SwiftUI), 요약(SwiftUI)
  - Wikipedia 첫 실행 온보딩 5쪽(SwiftUI `WMFAppOnboardingView`): 소개, 개인정보, 언어, 개인화 소개, 관심사(네트워크)
  - Wikipedia 홈(네트워크), 프로필(SwiftUI), 설정(SwiftUI `WMFSettingsView`), 설정 하위 SwiftUI 화면 2개: 검색
    (`WMFSearchSettingsView`), 문서의 저장 및 동기화(`WMFStorageAndSyncingSettingsView`)
- 네트워크 화면: 관심사 화면의 문서 버튼은 실행마다 네트워크에서 받은 무작위 문서다(두 실행에서 같은 문서 0개). 홈 피드는
  계층을 읽은 시점의 로딩 정도에 따라 요소 수가 73개, 157개로 달랐다. Wikipedia는 고정 네트워크 응답
  (`-WMFTestHTTPClientProfile fixture-strict`)을 `TEST || UITEST` 컴파일 조건에서만 쓰므로 이 Debug 빌드로는 고정할 수
  없다. 이 두 화면도 규칙 그대로의 합계에 넣었다. 이 둘을 뺀 합계는 결과를 본 뒤 정한 사후 분류라 참고로만 보인다.
  실행마다 같은 내용으로 비교할 수 있도록 로컬 화면인 프로필·설정·설정 하위 화면 2개를 더 측정했다.
- 대상 요소: `INTERACTIVE_TYPES`(`reproof/simple/hierarchy.py`)에 드는 요소. 판정 후보인 `staticText`는 따로 셌다.
- 안정성: run-a의 요소마다 `capture_evidence(run-a, 요소)` → `resolve(run-b, 근거)`. 유지율: baseline run-a 근거를 변형
  run-a 계층에서 해석했다. 유지율 = 맞게 찾은 수 / B에 정답 요소가 있는 수.
- 정답 요소(locator와 별개로 정함, 먼저 맞는 규칙): ① A·B에서 유일한 identifier → ② 서명(종류, identifier, label, 행 글자)이
  같은 요소. 행 글자는 요소를 감싼 가장 가까운 `cell` 안의 `staticText`들이고, 서명이 같은 요소가 여럿이면 순번으로
  맞춘다 → ③ 서명과 frame이 같은 요소 → ④ 같은 path·종류·label. 없으면 "B에 없음"이다.
  path를 맨 뒤에 둔 이유: 처음에 path를 먼저 썼더니 `ui-insert`에서 행이 밀려 같은 path에 다른 행의 "추가" 버튼이 와,
  실제로는 맞게 찾은 3건이 "잘못 해석"으로 잘못 세어졌다.
- 분류: 정답이 있고 해석 결과의 path가 같으면 `matched_by`로 센다(identifier는 strong). 정답이 있는데 못 찾으면 "실패",
  다른 path를 고르면 label이 같아도 "잘못 해석"이다. 정답이 없을 때 못 찾으면 "B에 없음"이고, 같은 path·종류(label만
  바뀐 자리)를 고르면 "B에 없음(같은 자리)", path와 label이 모두 다른 요소를 고르면 "잘못 해석"이다.

### 결과: 실행 간 안정성 — interactive

"빈 label로 찾음"은 `type-label-anchor`·`label-ordinal`로 찾았지만 label이 빈 요소의 수다.

| 화면 | 요소 | strong | type-label-anchor | label-ordinal | position-ordinal | 실패 | 잘못 해석 | B에 없음 | 빈 label로 찾음 | strong+tla+lo |
|---|---|---|---|---|---|---|---|---|---|---|
| mixedshop/cart | 5 | 1 (20.0%) | 4 (80.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 | 5 (100.0%) |
| mixedshop/options | 15 | 1 (6.7%) | 5 (33.3%) | 9 (60.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 8 | 15 (100.0%) |
| mixedshop/summary | 3 | 0 (0.0%) | 3 (100.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 | 3 (100.0%) |
| wikipedia/onboarding-intro | 2 | 2 (100.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 | 2 (100.0%) |
| wikipedia/onboarding-privacy | 3 | 3 (100.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 | 3 (100.0%) |
| wikipedia/onboarding-languages | 2 | 2 (100.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 | 2 (100.0%) |
| wikipedia/onboarding-personalization | 2 | 2 (100.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 | 2 (100.0%) |
| wikipedia/onboarding-interests (네트워크) | 67 | 3 (4.5%) | 39 (58.2%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 12 (17.9%) | 13 (19.4%) (같은 자리 13) | 0 | 42 (62.7%) |
| wikipedia/home (네트워크) | 11 | 2 (18.2%) | 9 (81.8%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 1 | 11 (100.0%) |
| wikipedia/profile | 9 | 1 (11.1%) | 3 (33.3%) | 5 (55.6%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 5 | 9 (100.0%) |
| wikipedia/settings | 25 | 0 (0.0%) | 13 (52.0%) | 12 (48.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 12 | 25 (100.0%) |
| wikipedia/settings-search | 8 | 1 (12.5%) | 0 (0.0%) | 7 (87.5%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 7 | 8 (100.0%) |
| wikipedia/settings-storage | 12 | 1 (8.3%) | 1 (8.3%) | 10 (83.3%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 10 | 12 (100.0%) |
| **합계(규칙 그대로: 모든 화면)** | 164 | 19 (11.6%) | 77 (47.0%) | 43 (26.2%) | 0 (0.0%) | 0 (0.0%) | 12 (7.3%) | 13 (7.9%) (같은 자리 13) | 43 | 139 (84.8%) |
| 합계(홈만 제외, 온보딩 관심사 포함) | 153 | 17 (11.1%) | 68 (44.4%) | 43 (28.1%) | 0 (0.0%) | 0 (0.0%) | 12 (7.8%) | 13 (8.5%) (같은 자리 13) | 42 | 128 (83.7%) |
| 합계(네트워크 화면 2개 사후 제외, 참고) | 86 | 14 (16.3%) | 29 (33.7%) | 43 (50.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 42 | 86 (100.0%) |
| 소계(사후 제외 범위 중 SwiftUI 화면) | 81 | 13 (16.0%) | 25 (30.9%) | 43 (53.1%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 42 | 81 (100.0%) |

홈 행은 run-a(73개)가 run-b(157개)보다 덜 로딩된 상태라 A→B 방향에서 모두 찾은 것처럼 보일 뿐이다.

요소의 근거 재료(run-a, interactive): identifier가 있는 요소는 Wikipedia 온보딩 네 쪽에서 9/9였지만, 프로필·설정·설정 하위
화면 54개 중에서는 3개뿐이다(`Profile Join Wikipedia Row` 1, 시스템이 붙이는 `BackButton` 2). 이 54개 중 34개(63%)는 identifier도 label도
없다. MixedShop은 23개 중 identifier 2개(`cart.clear`, `BackButton`), 둘 다 없는 요소 8개(옵션 화면).

### 결과: 실행 간 안정성 — staticText (판정 후보)

| 화면 | 요소 | strong | type-label-anchor | 그 밖 | B에 없음 | strong+tla+lo |
|---|---|---|---|---|---|---|
| MixedShop 3개 화면 | 13 | 0 | 13 | 0 | 0 | 13 (100%) |
| Wikipedia 로컬 화면 8개 | 47 | 1 | 46 | 0 | 0 | 47 (100%) |
| 합계(네트워크 화면 2개 사후 제외, 참고) | 60 | 1 (1.7%) | 59 (98.3%) | 0 | 0 | 60 (100%) |
| 온보딩 관심사(네트워크) | 76 | 0 | 40 | 0 | 36 (47.4%) | 40 (52.6%) |
| 홈(네트워크, run-a에 글자 없음) | 0 | 0 | 0 | 0 | 0 | 0 |
| **합계(규칙 그대로: 모든 화면)** | 136 | 1 (0.7%) | 99 (72.8%) | 0 | 36 (26.5%) | 100 (73.5%) |

로컬 화면의 글자는 모두 유일한 label로 찾았다. 다만 판정에 쓰는 글자는 값이 바뀌는 경우가 많다("합계 0" → "합계 1").
값이 바뀐 뒤에는 label로 찾을 수 없고 `position-ordinal`만 남는데, 이 경우는 이번에 측정하지 않았다(미검증, 계획 2에서
판정 근거를 정할 때 확인 필요).

### 결과: AI 수정 뒤 유지율

MixedShop 사본에 적용한 변경(`spikes/locator-coverage/edits.py`, 대상: 장바구니 "담기", 옵션의 상품 행 "추가"):

| 변형 | `CartViewController.swift` | `OptionsView.swift` |
|---|---|---|
| edit-above: 대상 위에 20줄 추가 | `makeAddButton` 위에 도우미 함수 2개(20줄, 호출 안 함) | `body` 위에 단가 표·합계 계산(20줄) |
| edit-other: 같은 화면 다른 버튼 로직 | "비우기" `clearTapped`에 빈 장바구니 guard, `setNeedsLayout` | "선물 포장" 토글에 `onChange`(끄면 메모 비움). 이 화면에 "추가" 말고 버튼이 없어 토글을 골랐다 |
| edit-action: 대상 action 변경 | `addTapped`에 최대 수량 guard | "추가" 클로저에 최대 수량 guard |
| edit-all(참고) | 위 세 가지 모두 | 위 세 가지 모두 |
| ui-insert(참고, 규칙 밖) | "담기" 위에 "쿠폰" 버튼 추가 | 상품 행 위에 "쿠폰 적용" 버튼 행 추가 |

| 변형 | 요소 | strong | type-label-anchor | label-ordinal | position-ordinal | 실패 | 잘못 해석 | 유지율(맞게 찾음/남은 요소) |
|---|---|---|---|---|---|---|---|---|
| edit-above | 23 | 2 | 12 | 9 | 0 | 0 | 0 | 23/23 (100%) |
| edit-other | 23 | 2 | 12 | 9 | 0 | 0 | 0 | 23/23 (100%) |
| edit-action | 23 | 2 | 12 | 9 | 0 | 0 | 0 | 23/23 (100%) |
| edit-all(참고) | 23 | 2 | 12 | 9 | 0 | 0 | 0 | 23/23 (100%) |
| ui-insert(참고) | 23 | 2 | 12 | 3 | 0 | 6 | 0 | 17/23 (73.9%) |

앞의 네 변형은 세 화면 계층 JSON의 내용이 baseline과 완전히 같았다. 그래서 이들의 23/23은 구성상 항상 통과하는 값이다
(화면에 보이지 않는 수정). 실제 AI 수정은 보이는 UI를 바꾸는 경우가 많으므로, 이 표에서 정보가 있는 값은 `ui-insert`의
17/23(잘못 해석 0, 실패 6은 모두 안전한 실패)뿐이다. `ui-insert`에서 "추가" 3개는 같은 label 3개라
`label-ordinal`로 맞게 찾았고, 실패 6개는 옵션 화면의 빈 label `cell` 6개(행이 6개에서 7개로 늘어 순번 근거를 건너뜀)다.
장바구니는 "담기"·"옵션"이 유일한 label이라 위치가 밀려도 그대로 찾았다.

### 발견 (계획 2에서 다룰 것, 이번에는 툴을 고치지 않음)

1. **XCUITest idle 대기 때문에 탭 하나가 최대 2분 걸린다.** Wikipedia 홈이 뜬 뒤로 앱이 idle이 되지 않아 XCUITest가
   탭 앞뒤로 60초씩 기다렸다(run마다 세션 로그에 `App animations complete notification not received` 6번). 홈·프로필·설정·
   검색 설정으로 가는 탭 4번이 61–121초 걸렸고, run 전체 510–517초 중 약 6분이 이 대기였다. MixedShop 탭은 1–3초였다.
   실행기 클라이언트의 기본 제한 시간 30초(`RunnerClient(timeout=30.0)`)보다 길어서, 탐색 중에는 `runner_unreachable`
   (`timed out`)가 났고, 실행기가 앞 탭을 계속 처리하는 동안 다음 `hierarchy` 명령도 시간 초과됐다. 측정 스크립트는 제한
   시간을 300초로 늘려 우회했다. 지금 배포된 명령(`inspect`)은 탭하지 않아 영향이 없다. 녹화·재생에서는 idle 대기를 줄이는
   방법(XCUITest 공개 API에는 없음, 검증 필요)과 명령별 제한 시간을 함께 정해야 한다.
2. **SwiftUI 요소는 label이 비는 경우가 많다.** List 행은 label 없는 `cell`과 그 안의 label 있는 `button`으로 나온다(설정).
   `Toggle("", isOn:).labelsHidden()`(Wikipedia `SettingsRow`)은 `switch`가 두 겹(바깥·안쪽)으로 나오고 둘 다 label이 없다.
   행 글자는 옆의 `staticText`에만 있다. MixedShop `Toggle("선물 포장", …)`은 바깥 `switch`에만 label이 있다. `TextField`는
   label이 비고 placeholder("메모")에만 글자가 있는데, locator는 placeholder를 쓰지 않는다.
3. **권고(추정, 측정 안 함):** label이 빈 요소에는 감싼 `cell`의 글자(위 정답 판정의 "행 글자")를 근거로 더하면 순번 의존이
   줄어든다. 정답 판정에 쓴 이 서명은 `ui-insert`에서도 옵션 화면 15개 요소의 짝을 모두 찾았다. 소스 재작성 방식도 한 소스
   위치에서 여러 행을 만드는 `ForEach`에는 행마다 다른 ID를 주려면 결국 실행 시점 값(데이터나 순번)이 필요하다(추정).
4. **`position-ordinal`의 잘못 고름**(위 판정의 주의 3): 관심사 화면에서 사라진 문서 버튼 25개 중 25개를 무엇인가로 풀었다(같은
   자리 13, 다른 자리 12). 목록 크기가 우연히 같아 건너뛰기 조건이 걸리지 않았다.

### 이 spike가 남긴 것

커밋: `spikes/locator-coverage/`(스크립트 3개, README), 이 절, 설계 문서 7장 spike 1의 잠정 결정. 툴 코드는 바꾸지 않았다.

이 Mac에 남은 것: `~/.reproof/spikes/locator-coverage/`(약 720 MB: MixedShop 빌드 6개, 변형 소스, 계층 JSON, 세션 로그,
`coverage-details.json`). 세션 폴더(`runner-session-*`)는 모두 정상 종료로 지워졌다. Simulator `reproof-mixedshop`,
`reproof-wikipedia-ios`는 켜 둔 상태다(`inspect`와 같은 동작).

## S3 — Wikipedia 빌드

### 판정

**된다.** 고정 SHA의 Wikipedia iOS에서 `reproof app init` → `build` → `inspect`가 끝까지 성공했다
(첫 화면 계층 저장, `--point`로 고른 버튼을 `strength=strong`으로 다시 찾음).

다만 그대로는 되지 않았고, 다음 세 가지를 거쳤다.

| # | 막힌 곳 | 분류 | 대응 |
|---|---|---|---|
| 1 | `Configurations/OpenSourceDebug.xcconfig` 없음(빌드 설정 단계 오류) | 검증 앱 설정 | 파일을 손으로 만듦 |
| 2 | iOS 27.1 SDK 전용 API 사용(컴파일 오류) | 검증 앱 설정(환경: Xcode 27.1 필요) | iOS 27.0에서 동작이 같은 작은 patch(2곳) |
| 3 | 앱이 실행 직후 종료(App Group 컨테이너 없음) | **툴 일반 문제** | 서명 끄기 → ad-hoc 서명으로 수정(`b83476e`) |

1·2는 Wikipedia와 이 Mac(Xcode 27.0)의 조합 문제다. 회사 개발자의 기존 checkout에는 1의 파일이 이미 있고
(작업 사본은 `.gitignore`와 상관없이 소스 폴더를 복사한다), Xcode 버전도 회사 CI와 맞출 것이므로 회사 앱에서는
보통 생기지 않는다(추정). 3은 App Group·위젯을 쓰는 회사 앱이면 대부분 겪을 문제라 툴을 고쳤다.

### 환경

- Mac: Apple M4 Pro, 12코어, 메모리 24 GB, macOS 26.7.2
- Xcode 27.0 (27A266a), iOS Simulator 런타임 iOS 27.0, Python 3.14.7, git 2.54.0
- 설치하지 않은 도구: SwiftLint, ClangFormat, Tuist, Ruby gem(승인 범위 밖). Wikipedia의 `scripts/setup`도 실행하지 않았다.
- 작업 공간: 기본값 `~/.reproof`(`REPROOF_HOME` 미설정)

### 고정 SHA와 받기

- 저장소: `https://github.com/wikimedia/wikipedia-ios.git`
- 커밋: `09fa0f380510aedc4af515729cb6331b1a4acfcb` (2026-10-30T23:13:32Z 기준 `main` HEAD).
  2026-11-01 `git ls-remote ... refs/heads/main`도 같은 값이었다.
- pin 파일: `examples/simple-ios/wikipedia/pin.json`

```
$ python3 scripts/simple-fetch-app.py --pin examples/simple-ios/wikipedia/pin.json --output ~/.reproof/sources/wikipedia-ios
Cloning into '~/.reproof/sources/wikipedia-ios'...
HEAD is now at 09fa0f3805 Merge pull request #6335 from wikimedia/T440007/discover-setup
받았습니다: ~/.reproof/sources/wikipedia-ios
  HEAD: 09fa0f380510aedc4af515729cb6331b1a4acfcb (pin과 같음)
```

17초, 파일 4,716개(`.git` 제외), 폴더 226 MB(`.git` 포함). 컨테이너는 `Wikipedia.xcodeproj` 하나,
`Package.resolved`는 `Wikipedia.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/`에 있다(원격 패키지 6개:
CocoaLumberjack, HCaptcha, rive-ios, RxSwift, SDWebImage, swift-log. 로컬 패키지: WMFComponents, WMFData,
WMFLocalizations, WMFTestKitchen).

### 측정 요약

| 항목 | 값 | 목표 |
|---|---|---|
| init 질문 수 | 2개 (앱 scheme 고르기 1, 테스트 scheme 확인 1) | — |
| init에서 사람이 직접 입력한 항목 | 2개 (`9`, `Wikipedia`) | 3개 이하 ✓ |
| init 밖에서 사람이 한 앱 준비 | 2건 (xcconfig 파일 만들기, Xcode 27.0용 patch) — 검증 앱 설정 | — |
| init 소요(빌드 제외) | 104.5초 (대화형, 답은 stdin으로 바로 줌) / 비대화형 시도 136.2초(선택 필요로 종료) | 10분 이내 ✓ |
| 패키지 해석 | 첫 build 24.9초, 이후 3.6–7.1초(작업 공간 캐시 재사용) | — |
| 앱 빌드(xcodebuild) | 성공 빌드 70.3초(서명 끔), 79.3초(ad-hoc 서명, 최종) | — |
| 빌드 성공 여부 | 성공 (검증 앱 설정 2건 + 툴 수정 1건 후) | — |
| inspect | 60.8초(빌드 재사용 포함), 첫 실행은 Simulator 생성·부팅·초기화·설치 약 34초 + 실행기 빌드 약 12초 | — |
| 첫 화면 interactive 요소 | 2개, 그중 identifier 있는 것 2개 (전체 요소 21개) | — |
| 설정부터 첫 inspect 성공까지 실제 경과 | 약 23분(15:04 받기 → 15:27 inspect 성공, 진단·툴 수정 포함) | 2시간 time-box ✓ |

참고:
- 빌드마다 새 DerivedData에서 처음부터 컴파일한다. `generic/platform=iOS Simulator`라 arm64와 x86_64를 모두
  만든다(Swift 컴파일 작업 1,467개 × 2, C/Objective-C 518개). 실행 파일: `x86_64 arm64`.
- 원격 패키지 6개는 이 Mac의 전역 SwiftPM 캐시(`~/Library/Caches/org.swift.swiftpm`)에서 가져왔다("(cached)").
  전역 캐시는 init의 `xcodebuild -list`가 먼저 채웠으므로, 첫 build의 24.9초는 완전히 빈 상태의 시간이 아니다.
  init 안에서 `-list`가 패키지를 해석하는 시간은 같은 소스 사본으로 따로 재서 약 58초였다.
- 빌드 로그 9.0 MB, 경고 450개. 출력 상한(64 MiB)까지 약 7배 여유가 있다.

### 실행 기록

모든 명령은 `~/.reproof/sources/wikipedia-ios`에서 `PYTHONPATH=<reproof checkout> python3 -m reproof app ...`으로
실행했다(`reproof` 명령은 설치하지 않음). 시각은 줄 앞 `[경과 시각]`이며 긴 로그는 줄였다.

#### init (비대화형) — 선택이 필요해 멈춤

```
$ reproof app init --non-interactive          # 136.2초, 종료 코드 2
문제: 앱 scheme 후보가 여러 개라 자동으로 고를 수 없습니다: Alpha, ContinueReadingWidget, Experimental,
Local Page Content Service & Announcements, NotificationServiceExtension, Performance Testing, RTL,
WidgetsExtension, Wikipedia, Wikipedia Stickers, WikipediaUITests
해결: `--non-interactive` 없이 실행해 직접 고르거나, 앞 후보 중 하나로 reproof.toml을 직접 작성하세요.
건너뜀: Update Languages (scheme 'Update Languages' 에서 앱(application) 타깃을 찾지 못했습니다.)
... (WMF, WMFComponents, WMFData, WMFDataMocks, WMFDataTestSupport, WMFLocalizations,
     WMFNativeLocalizations, WMFTestKitchen, Update Localizations — 모두 10줄)
```

scheme 21개 중 앱 타깃을 빌드하는 11개가 후보가 됐다. 후보별 앱 타깃을 따로 확인하니 9개가 `Wikipedia`
타깃이고(위젯·알림·스티커 확장 scheme은 호스트 앱을 함께 빌드하고, `WikipediaUITests`는 테스트 호스트로 빌드한다),
`Alpha`, `Experimental`은 별도 타깃이었다. Debug에서는 셋 다 번들 ID가 `org.wikimedia.wikipedia`다.

#### init (대화형) — 답 2개

```
$ printf '9\nWikipedia\n' | reproof app init  # 104.5초, 종료 코드 0
건너뜀: ... (위와 같은 10줄)
앱 scheme를 고르세요.
  1. Alpha
  ...
  9. Wikipedia
  ...
  11. WikipediaUITests
번호 입력 (1-11): 테스트 scheme은 'Performance Testing'(으)로 추정했습니다. Enter=수락, 다른 이름 입력, -=사용 안 함:
저장했습니다: ~/.reproof/sources/wikipedia-ios/reproof.toml
  프로젝트: Wikipedia.xcodeproj  scheme: Wikipedia  테스트 scheme: Wikipedia
  앱 번들 ID: org.wikimedia.wikipedia  Simulator: iPhone 16 (latest)
질문 2개
환경 점검:
✓ xcode: Xcode 27.0
✓ ios_runtime: iOS 27.0
✓ git: git 사용 가능
✓ disk: 남은 공간 105.8 GB
ⓘ claude: claude 사용 가능 (AI 수정에 필요)
다음 단계: reproof app build
```

테스트 scheme 추정값 `Performance Testing`은 틀렸다. Wikipedia의 단위 테스트(`WikipediaUnitTests`)는 앱 scheme
`Wikipedia`의 테스트 동작에 들어 있어 `Wikipedia`를 직접 입력했다. Enter로 수락해도 답은 2개로 같지만, 그러면 틀린 값이 저장된다.
`doctor`의 `claude` 항목은 PATH에서 찾기만 하고 실행하지 않는다.

#### build 1 — 실패: 생성 xcconfig 없음

```
[   0.1s 15:09:32] 빌드 식별 중
[   0.1s 15:09:32] 작업 사본 만드는 중
[   1.2s 15:09:33] 패키지 받는 중
[  26.1s 15:09:58] 앱 빌드 중
[  33.5s 15:10:06] 문제: xcodebuild 실행이 실패했습니다.
                   해결: 로그를 확인해 원인을 해결한 뒤 다시 실행하세요.
                   로그: ~/.reproof/projects/wikipedia-ios/builds/6ce97a492db35eca.partial/logs/build.log
```

로그의 원인 줄:

```
.../source/Wikipedia.xcodeproj:1:1: error: Unable to open base configuration reference file
'.../source/Configurations/OpenSourceDebug.xcconfig'.
```

#### build 2 — 실패: Xcode 27.1 SDK 필요

```
[   4.9s 15:11:40] 앱 빌드 중
[  23.7s 15:11:58] 문제: xcodebuild 실행이 실패했습니다.
```

```
.../WMFComponents/Sources/WMFComponents/Extensions/Navigation Bar/WMFNavigationBarConfiguring.swift:384:36:
error: value of type 'UITraitCollection' has no member 'verticalBarEdge'
...WMFNavigationBarConfiguring.swift:407:70: error: type 'UITraitCollection' has no member 'systemTraitsAffectingVerticalBarEdge'
```

#### build 3 — 성공(서명 끔)

```
[   1.3s 15:13:09] 패키지 받는 중
[   4.9s 15:13:13] 앱 빌드 중
[  75.2s 15:14:23] 빌드 완료: ~/.reproof/projects/wikipedia-ios/builds/9edc8552bee13db5/DerivedData/Build/Products/Debug-iphonesimulator/Wikipedia.app
[  75.2s 15:14:23] 빌드 ID: 9edc8552bee13db5
```

#### inspect 1 — 실패: 앱이 실행 직후 종료

```
[   1.9s 15:15:17] 빌드 완료: .../Wikipedia.app            # 재사용
[  89.7s 15:16:44] 문제: 실행기(127.0.0.1:63506)에 'launch' 명령을 보내지 못했습니다: timed out
                   해결: 실행기 세션이 살아 있는지 확인하고, 세션 로그를 본 뒤 다시 실행하세요.
```

세션 로그는 `Launch org.wikimedia.wikipedia` → `Setting up automation session`에서 멈췄다.
`~/Library/Logs/DiagnosticReports/Wikipedia-2026-11-01-151617.ips`(SIGABRT):
`-[NSURL initFileURLWithPath:isDirectory:]` ← `-[MWKDataStore setupLegacyDataStore]` ← `-[MWKDataStore initWithContainerURL:]`.
`WidgetsExtension-*.ips` 5개(EXC_BREAKPOINT)도 `SharedContainerCache.cacheDataFileURL.getter`에서 같은 원인으로 반복 종료했다.

#### build 4 — 성공(ad-hoc 서명, 최종)

툴 수정 뒤, 같은 빌드 ID의 완성 빌드가 재사용되지 않도록 `builds/9edc8552bee13db5`를 지우고 다시 빌드했다
(아래 "툴 수정"의 빌드 ID 항목에서 고침).

```
[   1.7s 15:24:28] 패키지 받는 중
[   8.8s 15:24:35] 앱 빌드 중
[  88.1s 15:25:55] 빌드 완료: .../builds/9edc8552bee13db5/DerivedData/Build/Products/Debug-iphonesimulator/Wikipedia.app
[  88.1s 15:25:55] 빌드 ID: 9edc8552bee13db5
```

`codesign -dv`: `flags=0x2(adhoc)`, 실행 파일에 `__TEXT,__entitlements`(App Group
`group.org.wikimedia.wikipedia` 포함)가 들어갔다. 이전 빌드는 `flags=0x20002(adhoc,linker-signed)`이고 이 영역이 없었다.

#### inspect 2·3 — 성공

```
$ reproof app inspect                          # 60.8초
[   2.1s 15:26:04] 빌드 완료: .../Wikipedia.app
[  60.2s 15:27:02] 화면 계층 저장: ~/.reproof/projects/wikipedia-ios/builds/9edc8552bee13db5/inspect-20261101-152702.json

$ reproof app inspect --point 355,788          # 60.5초
[  59.9s 15:28:21] 화면 계층 저장: .../inspect-20261101-152821.json
요소: 종류=button 라벨=다음 identifier=App Onboarding Next Button
근거: {"identifier": "App Onboarding Next Button", "elementType": "button", "label": "다음", "anchorIdentifier": "", "labelOrdinal": 0, "positionOrdinal": 0, "labelCount": 1, "positionCount": 1}
재탐색: strength=strong matched_by=identifier
```

이후 Wikipedia·WidgetsExtension 충돌 보고서는 새로 생기지 않았다.

### 첫 화면 계층

첫 실행 온보딩 소개 화면(한국어). 요소 21개 중 `INTERACTIVE_TYPES`(`reproof/simple/hierarchy.py`)에 드는 것은
`button` 2개이고 둘 다 identifier가 있다.

| 종류 | identifier | 라벨 | frame |
|---|---|---|---|
| button | `App Onboarding Learn More Link` | 위키백과에 대해 더 알아보기 | (32, 260.3, 177.7, 19.3) |
| button | `App Onboarding Next Button` | 다음 | (333, 766, 44, 44) |

그 밖에 `App Onboarding Intro View`(other), 이미지 2개, staticText 2개, 스크롤 막대 2개 등이 있다. 온보딩은
identifier가 잘 달린 화면이라, 앱 전체의 identifier 비율(설계 문서: 약 91개 파일)을 대표하지는 않는다.

### 검증 앱 설정 (Wikipedia 전용, 툴 기본값은 바꾸지 않음)

1. **`Configurations/OpenSourceDebug.xcconfig`**: 프로젝트 Debug 구성의 base configuration인데 `.gitignore`
   82번 줄에 있어 저장소에 없다. Wikipedia의 `scripts/setup_bundle_id`가 만들고, Xcode Cloud는
   `ci_scripts/ci_pre_xcodebuild.sh`에서 `scripts/setup_bundle_id ci`를 실행한다. 이번에는 스크립트를 실행하지 않고
   `ci` 모드와 같은 내용을 원본 사본(`~/.reproof/sources/wikipedia-ios`)에 직접 썼다.

   ```
   // This file was automatically generated, do not edit directly.

   DEVELOPMENT_TEAM=AKK7J2GV64

   SIGNING_DISAMBIGUATOR=
   ```

   `.gitignore` 대상이라 빌드 식별의 "작업 중 변경"에는 나오지 않지만, 작업 사본에는 복사되므로 소스 지문(build ID)에는 반영된다.

2. **Xcode 27.1 SDK 필요**: 커밋 `cbbfddf749`(2026-10-23, "Use the native large title when the system shows a
   vertical bar")가 iOS 27.1 SDK에만 있는 `UITraitCollection.verticalBarEdge`와
   `systemTraitsAffectingVerticalBarEdge`를 타입으로 쓴다. Wikipedia의 GitHub Actions도 `XCODE_VERSION: "27.1"`을 쓴다.
   같은 저장소의 `UIBarButtonItem+SwiftUI.swift`는 "CI builds with an older Xcode" 때문에 KVC로 우회하는데 이 파일은 그렇지
   않다. 이 Mac은 Xcode 27.0이고 Xcode 설치는 승인 범위 밖이라, 원본 사본에 아래 patch를 적용했다. iOS 27.1 미만
   런타임에서는 원래 코드도 `false`를 돌려주거나 바로 `return`하므로, iOS 27.0 Simulator에서 동작은 같다.
   빌드 식별에는 `commit 09fa0f3 + 작업 중 변경`으로 남는다(`dirty.patch`, `reproof.toml`은 추적 안 됨으로 기록).

   ```diff
   --- a/WMFComponents/Sources/WMFComponents/Extensions/Navigation Bar/WMFNavigationBarConfiguring.swift
   +++ b/WMFComponents/Sources/WMFComponents/Extensions/Navigation Bar/WMFNavigationBarConfiguring.swift
   @@ -380,9 +380,7 @@
        private var hasVerticalBar: Bool {
   -        if #available(iOS 27.1, *) {
   -            return traitCollection.verticalBarEdge != .unspecified
   -        }
   +        // reproof 검증 앱 설정: Xcode 27.0 SDK에는 verticalBarEdge가 없다. iOS 27.1 미만에서는 원래도 false다.
            return false
        }
   @@ -402,12 +400,8 @@
        private func observeVerticalBarEdgeChanges(title: String) {
   -        guard #available(iOS 27.1, *) else { return }
   -        guard WMFVerticalBarEdgeObservers.registrations.object(forKey: self) == nil else { return }
   -        let registration = registerForTraitChanges(UITraitCollection.systemTraitsAffectingVerticalBarEdge) { (viewController: Self, _) in
   -            viewController.applyCustomLeadingLargeTitle(title)
   -        }
   -        WMFVerticalBarEdgeObservers.registrations.setObject(registration, forKey: self)
   +        // reproof 검증 앱 설정: Xcode 27.0 SDK에는 systemTraitsAffectingVerticalBarEdge가 없다. iOS 27.1 미만에서는 원래도 바로 return한다.
   +        return
        }
   ```

   다음 작업(심은 버그 등)을 위한 선택지: Xcode 27.1 설치를 승인받거나, 이 patch를 검증 앱 patch로 함께 관리하거나,
   `cbbfddf749` 이전 커밋으로 pin을 옮긴다.

### 툴 수정 (일반 문제)

- **Simulator 빌드 서명**(`b83476e fix: Simulator 빌드를 서명 끄기 대신 ad-hoc 서명으로 바꿔 entitlement 유지`):
  `CODE_SIGNING_ALLOWED=NO`면 entitlement가 실행 파일에 들어가지 않아 App Group 컨테이너가 `nil`이 되고, 이를 가정한
  앱·확장이 시작하자마자 죽는다. `simulator_build_argv`가 `CODE_SIGN_IDENTITY=-`(Sign to Run Locally)를 쓰게 바꿨다.
  키체인 인증서와 프로비저닝 프로필은 쓰지 않는다(빌드 로그: `Signing Identity: "Sign to Run Locally"`,
  `TeamIdentifier=not set`). 자동 서명·`DEVELOPMENT_TEAM`이 있는 Wikipedia에서도 오류 없이 빌드됐다.
  포터블 테스트 `test_build_argv_ad_hoc_signs_so_simulator_entitlements_are_embedded`를 먼저 실패시킨 뒤 고쳤고,
  MixedShop native 테스트(`test_simple_native_build`, `test_simple_native_cli`)도 같은 설정으로 통과했다.
  실행기(`ios/SimpleRunner`) 빌드는 entitlement가 없어 그대로 둔다.
- **빌드 ID에 빌드 방법과 Xcode 버전 포함**(`1ceb6e3`): 서명 방식을 고친 뒤에도 빌드 ID가 `9edc8552bee13db5`로
  같아, 그대로 두면 App Group이 빠진 이전 앱을 재사용했다(위 build 4는 손으로 지운 뒤 빌드). 이제 빌드 식별 문서에
  `buildRecipe`(`reproof/simple/build.py`의 `BUILD_RECIPE_VERSION`, ad-hoc 서명부터 2)와 `xcodeVersion`
  (`xcodebuild -version` 출력)이 들어간다. 수정 뒤 같은 소스로 다시 build하니 새 빌드 ID `80392764bf5f8c1b`로
  처음부터 빌드했다(빌드 78.5초).
- **init이 전역 DerivedData에 고아 폴더를 남기지 않게 함**(`497db49`): init의 `xcodebuild -list`가 패키지를 해석하며
  `~/Library/Developer/Xcode/DerivedData/Wikipedia-<해시>/SourcePackages`(1.1 GB)를 init마다 새로 만들었다(이번
  spike에서 2개, 2.2 GB). `-list`는 `-derivedDataPath`를 받지 않으므로(`The flag -scheme, -testProductsPath, or
  -xctestrun is required when specifying -derivedDataPath.`) 같은 효과의 사용자 기본값
  `-IDECustomDerivedDataLocation=<init 임시 폴더>/dd`를 주고, `-showBuildSettings`에는 `-derivedDataPath`를 준다.
  패키지는 둘 다 `-clonedSourcePackagesDirPath <작업 공간>/cache/spm/discovery`로 받는다. 이 사용자 기본값은 그
  실행에만 적용되고 Xcode 설정에 저장되지 않음을 확인했다. MixedShop `init --non-interactive` 전후로 전역 DerivedData
  목록이 같았다(`-list`에 패키지 폴더만 주던 중간 구현에서는 8 KB `Logs` 폴더가 하나 생겼다).
  남은 것: build의 `-resolvePackageDependencies`는 빌드 ID마다 8 KB `Logs` 폴더를 전역에 남긴다(이번 수정 범위 밖).

### 툴 발견 (이번에는 고치지 않음, 후속 판단 필요)

1. **앱 scheme 후보가 많다.** 11개 후보가 실제로는 앱 타깃 3개였다(9개가 `Wikipedia`). 권고: (타깃, 번들 ID)가
   같은 후보를 묶고, 컨테이너·타깃 이름과 같은 scheme(`Wikipedia`)을 기본값으로 보여 Enter로 수락하게 한다.
2. **테스트 scheme 추정이 틀렸다.** `"Test" in name` 규칙이 `Performance Testing`, `WMFDataTestSupport`,
   `WMFTestKitchen`을 후보로 잡고 정렬상 첫 값을 골랐다. 앱 scheme 자신이 테스트 동작을 가진 경우(Wikipedia)를
   우선하고, `Tests`로 끝나는 이름을 우선하는 것을 권고한다.
3. **init 출력의 "건너뜀" 10줄**은 앱 타깃이 없는 패키지·스크립트 scheme이라 정상인데 오류처럼 보인다. 한 줄 요약을 권고한다.
4. **실패한 빌드 폴더가 쌓인다.** 소스가 바뀌어 빌드 ID가 달라지면 이전 `*.partial`은 지워지지 않는다
   (이번에 166 MB, 983 MB가 남음).
5. **빌드 실패 안내가 원인을 보여 주지 않는다.** 두 실패 모두 원인은 로그의 `error:` 줄이었는데 화면에는
   "xcodebuild 실행이 실패했습니다"만 나온다. 첫 몇 개의 서로 다른 `error:` 줄을 함께 보여 주는 것을 권고한다.
   특히 `Unable to open base configuration reference file '<경로>'`는 "저장소에 없는(보통 설정 스크립트가 만드는)
   xcconfig가 필요하다"는 안내로 바꿀 수 있다.
6. **앱이 실행 직후 죽으면 안내가 엉뚱하다.** inspect 1은 `'launch' 명령을 보내지 못했습니다: timed out`으로만
   끝났다. 앱 충돌 보고서(`DiagnosticReports/<앱 이름>-*.ips`)나 앱 상태를 확인해 "앱이 실행 직후 종료됐습니다"로
   알리는 것을 권고한다.
7. **진행 표시가 없는 구간**: `build.log`는 xcodebuild가 끝나야 써지고, inspect의 Simulator 준비·실행기 빌드(첫 실행 약 46초)
   동안 출력이 없다. Wikipedia는 빌드가 80초 안팎이라 문제가 작지만, 더 큰 앱에서는 멈춘 것처럼 보일 수 있다.
8. **두 아키텍처 빌드**: generic Simulator destination이라 arm64·x86_64를 모두 컴파일한다. Apple Silicon에서 arm64만
    빌드하면 시간이 줄 것으로 추정하지만 측정하지 않았다(측정 뒤 결정).

### 경고 (첫 줄만)

- `warning: SwiftLint not installed, run "scripts/brew_install" script to install` — SwiftLint 단계는 경고만 내고 지나간다.
- `warning: tasks in 'Copy Headers' are delayed by unsandboxed script phases; set ENABLE_USER_SCRIPT_SANDBOXING=YES to enable sandboxing (in target 'WMF' from project 'Wikipedia')`
- `note: Run script build phase 'SwiftLint' will be run during every build because the option to run the script phase "Based on dependency analysis" is unchecked.` (`Update Localizations`, `Run Script`도 같음)
- 컴파일 경고 상위: `cannot find protocol definition for 'WMFBackgroundFetcher'`(86), `'weak' ownership of capture 'self' differs ...`(54), `'WMFAuthButton' is deprecated ...`(28). 전체 450개, 빌드에는 영향 없음.
- Run Script 단계(`Update Localizations`, `Copy UI Test Network Fixtures` 등)는 추가 도구 없이 통과했다.

### 매크로·플러그인 검증 실패 감지 (Task 7 이월 항목)

실제 "must be enabled" 문구는 **관찰하지 못했다**. 빌드·패키지·세션 로그 7개에서 `must be enabled`는 0건이다.
Wikipedia는 `Package.resolved`가 있어 버전 고정 빌드(`-skipMacroValidation -skipPackagePluginValidation`)로 돌았고,
원격 패키지 6개와 로컬 패키지 4개의 `Package.swift`에 `.macro(`, `.plugin(`, `CompilerPluginSupport`가 없다.
따라서 `reproof/simple/build.py`의 `is_validation_failure`는 실제 문구로 아직 검증되지 않았다. 검증하려면 고정되지 않은
매크로 패키지가 필요한데, 대부분 SwiftSyntax를 받아야 해서 승인 범위 밖이다.

### 이 spike가 남긴 것

저장소에 커밋한 것: `examples/simple-ios/wikipedia/pin.json`, `scripts/simple-fetch-app.py`와 테스트, 이 문서, 툴 수정.
Wikipedia 소스와 빌드 산출물은 커밋하지 않았다.

이 Mac에 남은 것(정리 여부는 사용자 판단):

- `~/.reproof/sources/wikipedia-ios` — 받은 소스 + `reproof.toml` + 위 xcconfig·patch
- `~/.reproof/projects/wikipedia-ios/builds/` — 완성 빌드 2개(빌드 ID 수정 전 `9edc8552bee13db5`, 수정 후
  `80392764bf5f8c1b`, 각 약 3.8 GB), 실패한 partial 2개(1.1 GB)
- `~/.reproof/cache/spm/` 1.1 GB, `~/.reproof/runner/` 155 MB
- `~/Library/Developer/Xcode/DerivedData/Wikipedia-*` 7개(init이 만든 1.1 GB짜리 2개, build의 패키지 해석과 init 수정 전
  확인 실험이 만든 8 KB짜리 5개). MixedShop 확인 중 생긴 `MixedShop-*`도 8 KB짜리가 하나 늘었다.
- `~/Library/Caches/org.swift.swiftpm/repositories`의 Wikipedia 의존성 6개(Xcode가 평소 쓰는 공용 캐시)
- `~/Library/Logs/DiagnosticReports/`의 `Wikipedia-2026-11-01-151617.ips`, `WidgetsExtension-2026-11-01-1516*.ips` 5개
- Simulator `reproof-wikipedia-ios`(켜 둠, 문서의 기본 동작). 다른 Simulator는 건드리지 않았다.
- inspect 1 실패 때 남은 세션 폴더(토큰이 들어 있을 수 있음)는 빌드 폴더 `9edc8552bee13db5`와 함께 지웠다.
