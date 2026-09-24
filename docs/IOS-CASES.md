# iOS 버그 사례와 실제 AI 수정

세 가지 독립적인 제품 결함을 같은 UIKit 샘플에서 실행한다. case는 launch fixture로 고정되며 녹화 중 바뀌지 않는다. 각 사례는 수정 파일·oracle·보호된 회귀 테스트가 따로 있다.

| case | 기록 절차 | 원본 증상 → 기대 결과 | 수정 허용 파일 |
|---|---|---|---|
| `counter` | QA 입력 → Add | count 2 → 1 | `CounterLogic.swift` |
| `duplicate-submit` | QA 입력 → Submit 두 번 | 둘 다 승인되어 count 2 → 첫 제출만 승인하여 1 | `SubmissionLogic.swift` |
| `reset` | QA 입력 → Add → Reset | 이전 count 1 유지 → 0으로 초기화 | `ResetLogic.swift` |

중복 제출과 reset은 CounterLogic을 재사용해 같은 버그를 다른 이름으로 검사하지 않는다. 각자의 제품 함수를 사용하며, 다른 사례의 의도된 버그는 해당 패치가 변경할 수 없다.

## 실행

부팅된 iOS Simulator UUID와 새 출력 경로를 지정한다.

```bash
# 실제 Claude 호출: 샘플의 해당 제품 소스와 합성 기록만 전송
bash scripts/ios-cases.sh <SIMULATOR_UUID> artifacts/my-ai-cases claude

# 준비된 기준 패치로 실행하는 오프라인 통합 검사
bash scripts/ios-cases.sh <SIMULATOR_UUID> artifacts/my-offline-cases offline
```

개별 사례:

```bash
python3 -m reproof ios-build --simulator <SIMULATOR_UUID> --output artifacts/cases-build
python3 -m reproof ios-record --case duplicate-submit --simulator <SIMULATOR_UUID> \
  --build artifacts/cases-build --output artifacts/submission-record
python3 -m reproof ios-repair artifacts/submission-record/bundle --simulator <SIMULATOR_UUID> \
  --agent claude --output artifacts/submission-repair
```

`--case`는 `counter`, `duplicate-submit`, `reset`만 허용한다. 앱의 미지정 case는 counter이지만, 지정된 알 수 없는 값은 정상 사례로 fallback하지 않는다. 기존 Android v1의 입력 허용 범위는 확장하지 않았다.

## 판정과 패치 보호

모든 사례는 원본 3/3 같은 결함 → 실제 AI 제안 → 같은 구성 재빌드 → 사례별 보호된 회귀 검사 → 수정본 3/3 정상 순서다. 원본 UI runner를 고정하여 사용하고, 완료 직전 원본·패치 소스·policy·runner·앱 artifact를 다시 확인한다.

Swift 패치는 제한된 반환 표현식만 허용한다. 카운터는 정수, 중복 제출은 `true`/`false`/`!submitted`, reset은 `previous`/정수다. 허용 문법에 맞아도 잘못된 결과는 회귀·UI 검사에서 실패한다. 에이전트가 build 설정·fixture·ID·테스트를 바꾸는 것은 차단한다.

`agentReceipt`에는 요청 모델·도구 비활성화 여부·요청/응답 digest·시각·완료 상태만 저장한다. 토큰이나 계정 인증값을 기록하지 않는다. 실제 채택한 수정은 각 attempt의 `edits.json`과 `patch.diff`로 확인할 수 있다.

회귀 검사는 사례별 테스트 한 개를 실행한다. 한 사례의 `verified`는 다른 의도적 결함까지 모두 고쳤다는 의미가 아니다. 전체 조합의 상태는 사례별 결과를 함께 확인해야 한다.

## 결과와 범위

실행 결과는 각 case의 `record/`, `repair/job.json`, `repair/report.html`에 남는다. 작업 중 생성된 결과 묶음은 `artifacts/ios-ai-cases/`에 있다. [종합 HTML](../artifacts/ios-ai-cases/index.html)과 [검증된 JSON 집계](../artifacts/ios-ai-cases/validated-summary.json)에 결과를 기록했다. 세 사례 모두 첫 Claude 제안으로 원본 3/3 재현·회귀 검사 1/1·수정본 3/3을 통과했다.

이 검증은 합성 QA 데이터를 사용하는 Simulator 결과다. 실제 iPhone 서명·기기별 동작, 운영 데이터, 임의 앱 수집, 임의 Swift 코드 수정은 포함하지 않는다.
