# Android Live 녹화에서 AI 수정·실기기 검증까지

명시적 앱 프로필 연결 기반은 [Android 앱 프로필](ANDROID-APP-PROFILES.md)에 별도로 기록했다.
아래 실기기 결과는 샘플 통합 당시의 소스·빌드에 대한 근거이며, 새 프로필 경로의 물리 기기 검증을 의미하지 않는다.

Android 샘플의 같은 앱 실행에서 Raw Live 입력과 SDK 의미 이벤트를 수집하고,
기존 보호된 `repair` 작업으로 원본 재현·Claude 수정·회귀 검사·수정본 검증을 수행한다.
지원 fixture는 카운터 샘플이다.

## 실행

먼저 `python3 -m reproof live-device-doctor`로 연결을 확인한다.
각 output에는 새 디렉터리를 사용한다.

```bash
python3 -m reproof build --source android --variant buggy \
  --output artifacts/android-live-repair-build

# 기존 설치된 Android 도구와 Gradle 캐시를 사용한다.
cd android
JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home \
ANDROID_HOME="$HOME/Library/Android/sdk" \
./gradlew --offline --no-daemon :live:assembleDebug
cd ..

python3 -m reproof live-serve --android auto \
  --android-helper android/live/build/outputs/apk/debug/live-debug.apk \
  --android-app artifacts/android-live-repair-build/original.apk \
  --repair-source android --repair-build artifacts/android-live-repair-build \
  --repair-agent claude --output artifacts/android-live-repair-console
```

브라우저에서 Android 기기를 선택하고 **Start session**을 누른다.
**Reset & record** → 앱의 Name 선택 → 콘솔에서 `QA` 또는 `Test` 전송 → 앱의 Add를 한 번 누른다.
count `2`를 확인한 뒤 **Stop** → **Analyze & repair recording**을 누른다.
작업이 `verified`가 되면 **Open repaired app**으로 새 세션을 열고 같은 입력에서 count `1`을 확인한다.

`--repair-agent claude`는 설정한 샘플 제품 소스와 허용된 QA 이벤트·oracle의 외부 전송을 활성화한다.
서버 요청으로 다른 소스 경로나 모델 주소를 지정할 수 없다. 화면 이미지는 로컬에 보관한다.

## 검증 계약

- `build --output`은 원본 APK·재생 드라이버 APK와 두 빌드의 소스 digest를 새 디렉터리에 고정한다.
  기존 `build --receipt` 명령도 유지한다.
- Live는 SDK 기록 모드로 앱을 시작하고 매 reset에서 실행 ID를 새로 고정한다.
  다른 실행의 기록, 미완성 SDK 기록, 허용하지 않은 텍스트를 거절한다.
  캡처는 Live helper가 샘플의 Report를 누르고 SDK 결과를 읽으며, 동시 재생 드라이버를 띄우지 않는다.
- 화면 분석은 샘플 접근성 ID의 단일·가시적인 count `2` 관찰이다.
  Raw 녹화 digest와 frame ID를 함께 보존한다. 일반 화면 AI 탐색 기능이 아니다.
- 녹화 후 추가 입력·reset·앱 또는 원본 소스/빌드 변경과 오래된 controller를 거절한다.
  캡처 단계 실패 시 수동 제어를 복원한다.
- Live helper를 정상 종료한 뒤 기기를 예약한다. 원본을 3회 재현하고,
  Claude는 `CounterLogic.kt`의 제한된 숫자 표현식만 제안한다.
  보호된 단위 테스트 실행 후 수정본을 같은 기기에서 3회 검증한다.
- 매 원본·수정본 실행 전후 실제 설치된 재생 드라이버와 고정 APK digest를 확인한다.
  후보의 소스·APK·녹화·기기·회귀 검사·실제 Claude 요청 증거가 일치해야 재개를 허용한다.
- 검증 후보는 원본을 덮어쓰지 않는 별도 디렉터리에 남는다.
  재개는 작업에 연결된 새 Live 세션이며, APK가 바뀌면 재개를 거절한다.
  취소 후 정리가 확인되지 않으면 기기를 격리한다.

## 실기기 검증 결과

2026-09-11, 연결된 Android에서 전체 흐름을 확인했다.
브라우저의 pointer down/up 두 쌍과 텍스트 전송으로 Raw 이벤트 5개를 녹화했고,
같은 SDK 실행에서 `replace(name, QA)`와 `tap(add)` 두 이벤트가 수집됐다.
실제 Claude 첫 제안은 카운터 증가식을 `1`로 수정했다.

| 검증 | 결과 |
|---|---|
| 원본 실기기 재현 | 3/3, count `2` |
| 보호된 로직 테스트 | 1 통과, 실패·오류·skip 0 |
| 수정본 실기기 검증 | 3/3, count `1` |
| 원본 재생 드라이버 | 6개 실행 모두 설치 전후 동일 digest |
| 브라우저 후보 재개 | 새 세션에서 `QA` + Add → count `1` |
| 최종 증거 검사 | 현재 코드의 APK·fixture·driver·source·bundle 검사 통과 |
| 종료 | 세션·브라우저·서버·샘플·helper 종료, ADB forward 및 기기 점유 해제 |

- [실제 수정 보고서](../artifacts/android-live-repair-console/repairs/2b276018b2a14754aa65556e60744422/repair/report.html)
- [실제 Claude 패치](../artifacts/android-live-repair-console/repairs/2b276018b2a14754aa65556e60744422/repair/attempt-1/patch.diff)
- [수정 전 브라우저](../artifacts/android-live-repair-before.png)
- [수정 후 브라우저](../artifacts/android-live-repair-after.png)
- [후보 화면·세션·cleanup 증거](../artifacts/android-live-repair-final/result.json)
- [현재 증거 검사 결과](../artifacts/android-live-repair-current-gates.json)
- [종합 검증 결과](../artifacts/android-live-repair-validation.json)

Python 테스트 197개, 웹 테스트 15개가 통과했다. iPhone 공유 수정 경로는 Python 회귀 테스트로 확인했으며,
이번 세션에 iPhone 실기기 검증은 반복하지 않았다.

서버 재시작 후 진행 중 작업은 자동 재개하지 않는다. 검증 이력만으로 기존 후보를 다시 설정하지도 않는다.
저장된 후보를 별도로 열려면 해당 후보 APK를 `--android-app`으로 명시하고 새 Live output을 사용한다.
일반 앱의 임의 수정, 추가 Android fixture, 외부 worker의 수정 작업은 남은 범위다.
